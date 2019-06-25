import bz2
import importlib
import os.path
import pathlib
from typing import Iterator, List
import urllib.request
import urllib.error

from . import env
from .arch import arm
from .patch import Patch, RemotePatch
from .source import Source, GitSource, URLSource
from .util import BASE, run_in_dir, target_arch, system


class Package:
    BUILDDIR = BASE / 'build'
    DIST_PATH = BUILDDIR / 'dist'

    version: str = None
    source: Source = None
    patches: List[Patch] = []
    dependencies = []
    skip_uploading: bool = False
    re_configure: bool = False
    use_gcc: bool = False

    def __init__(self):
        self.name = type(self).__name__.lower()
        self.arch = target_arch().__class__.__name__

        if self.version is None and isinstance(self.source, GitSource):
            self.version = 'git'

        self.init_build_env()

        for patch in self.patches:
            patch.package = self

        for directory in (self.DIST_PATH, self.destdir()):
            directory.mkdir(exist_ok=True, parents=True)

    @property
    def sources(self) -> List[Source]:
        ret = []
        if self.source:
            ret.append(self.source)
        ret.extend([
            URLSource(patch.url)
            for patch in self.patches if isinstance(patch, RemotePatch)])
        return ret

    @classmethod
    def destdir(cls) -> pathlib.Path:
        return cls.BUILDDIR / 'target' / cls.__name__.lower()

    def init_build_env(self):
        self.env = {}
        self.env['CLANG_FLAGS_QPY']=os.getenv('CLANG_FLAGS_QPY','')
        self.env['CC_FLAGS_QPY']=os.getenv('CC_FLAGS_QPY','')
        

        ANDROID_NDK = self._check_ndk()

        self.env['ANDROID_NDK'] = ANDROID_NDK
        self.env['ANDROID_NDK_GFORTRAN'] = os.getenv('ANDROID_NDK_GFORTRAN')

        HOST_OS = os.uname().sysname.lower()

        if HOST_OS not in ('linux', 'darwin'):
            raise Exception(f'Unsupported system {HOST_OS}')

        self.TOOL_PREFIX = (ANDROID_NDK / 'toolchains' /
                            target_arch().ANDROID_TOOLCHAIN /
                            'prebuilt' / f'{HOST_OS}-x86_64')
        CLANG_PREFIX = (ANDROID_NDK / 'toolchains' /
                        'llvm' / 'prebuilt' / f'{HOST_OS}-x86_64')


        LLVM_BASE_FLAGS = [
            '-target', target_arch().LLVM_TARGET,
            '-gcc-toolchain', self.TOOL_PREFIX,
        ]

        ARCH_SYSROOT = (ANDROID_NDK / 'platforms' /
                        f'android-{env.android_api_level}' /
                        f'arch-{self.arch}' / 'usr' )

        UNIFIED_SYSROOT = ANDROID_NDK / 'sysroot' / 'usr'
        TARGET_ARCH_ANDROID = os.getenv('TARGET_ARCH_ANDROID')
        TARGET_ARCH_NAME = os.getenv('TARGET_ARCH_NAME')

        cflags = ['-fPIC', '-I'+str(ANDROID_NDK)+"/sysroot/usr/include", '-I'+str(ANDROID_NDK)+"/sysroot/usr/include/"+TARGET_ARCH_ANDROID+"-linux-"+TARGET_ARCH_NAME]

        if isinstance(target_arch(), arm) and not self.use_gcc: #use_gcc instead of clang
            cflags += ['-fno-integrated-as', '-v', '-femulated-tls']

        self.env.update({
            'ANDROID_API_LEVEL': env.android_api_level,

            # Sysroots
            'ANDROID_NDK': ANDROID_NDK,
            'ARCH_SYSROOT': ARCH_SYSROOT,
            'UNIFIED_SYSROOT': UNIFIED_SYSROOT,

            # Compilers
            'CC': f'{CLANG_PREFIX}/bin/clang',
            'CXX': f'{CLANG_PREFIX}/bin/clang++',
            'CPP': f'{CLANG_PREFIX}/bin/clang -E',

            'CPPFLAGS': LLVM_BASE_FLAGS + [
                f'--sysroot={ARCH_SYSROOT}',
                '-isystem', f'{UNIFIED_SYSROOT}/include',
                '-isystem', f'{UNIFIED_SYSROOT}/include/{target_arch().ANDROID_TARGET}',
                f'-D__ANDROID_API__={env.android_api_level}',
                '-D__ANDROID__=21'
            ],
            'CFLAGS': cflags,
            'CXXFLAGS': cflags,
            'LDFLAGS': LLVM_BASE_FLAGS + [
                '--sysroot=' + str(ARCH_SYSROOT),
                '-pie',
                '-lc',
                '-lm',
            ],
        })

        for dep in self.dependencies:
            dep_pkg = import_package(dep)
            self.env['CPPFLAGS'].extend(['-I', f'{dep_pkg.destdir()}/usr/include'])
            self.env['LDFLAGS'].extend(['-L', f'{dep_pkg.destdir()}/usr/lib'])

        for prog in ('ar', 'as', 'ld', 'objcopy', 'objdump', 'ranlib', 'strip', 'readelf'):
            self.env[prog.upper()] = self.TOOL_PREFIX / 'bin' / f'{target_arch().ANDROID_TARGET}-{prog}'

    @property
    def filesdir(self) -> pathlib.Path:
        return BASE / 'mk' / self.name

    def fresh(self) -> bool:
        if self.re_configure:
            return True

        if not self.source:
            return
        return not (self.source.source_dir / 'Makefile').exists()

    def run(self, cmd: List[str]) -> None:
        self.source.run_in_source_dir(cmd)

    def run_with_env(self, cmd: List[str]) -> None:
        self.source.run_in_source_dir(cmd, env=self.env)

    #def run_with_env_ldflags2(self, cmd: List[str]) -> None:
    #    self.env['LDFLAGS'] = self.env['LDFLAGS2']
    #    self.source.run_in_source_dir(cmd, env=self.env)

    def _check_ndk(self) -> pathlib.Path:
        ndk_path = os.getenv('ANDROID_NDK')
        if not ndk_path:
            raise Exception('Requires environment variable $ANDROID_NDK')
        ndk = pathlib.Path(ndk_path)

        #if not (ndk / 'sysroot').exists():
        #    raise Exception('Requires Android NDK r14 beta1 or above')

        return ndk

    def prepare(self):
        raise NotImplementedError

    def build(self):
        raise NotImplementedError

    def system(self, cmd: str) -> None:
        cmd = "cd %s && %s" % (self.source.source_dir, cmd)
        return system(cmd)

    def create_tarball(self):
        print(f'Creating {self.tarball_name} in {self.DIST_PATH}...')

        run_in_dir(['tar', '-jcf', self.tarball_path, '.'], cwd=self.destdir())

    @property
    def bintray_version(self):
        return f'{self.arch}-{self.version}'

    @property
    def bintray_path(self):
        return f'{env.bintray_username}/{env.bintray_repo}/{self.tarball_name}.bz2'

    @property
    def tarball_name(self):
        return f'{self.name}-{self.bintray_version}.tar.bz2'

    @property
    def tarball_path(self):
        return self.DIST_PATH / self.tarball_name

    def upload_tarball(self):
        if self.skip_uploading:
            print(f'Skipping uploading for package {self.name}')
            return

        bintray_api_key = os.getenv('BINTRAY_API_KEY')
        if not bintray_api_key:
            print(f'No Bintray API key found. Skipping uploading {self.tarball_name}...')
            return

        print(f'Uploading {self.tarball_name} to Bintray...')

        with open(self.tarball_path, 'rb') as pkg:
            import requests
            r = requests.put(
                f'https://bintray.com/api/v1/content/{self.bintray_path}',
                data=bz2.compress(pkg.read()),
                auth=(env.bintray_username, bintray_api_key),
                headers={
                    'X-Bintray-Package': self.name,
                    'X-Bintray-Version': self.bintray_version,
                    'X-Bintray-Publish': '1',
                    'X-Bintray-Override': '1',
                })

            print(f'Uploading result: {r.text}')

    def fetch_tarball(self):
        if self.skip_uploading:
            print(f'Skipping fetching package {self.name}')
            return

        if self.tarball_path.exists():
            print(f'Skipping already downloaded {self.tarball_path}...')
            return True

        tarball_url = f'https://dl.bintray.com/{self.bintray_path}'
        try:
            print(f'Downloading {tarball_url}...')
            req = urllib.request.urlopen(tarball_url)
        except urllib.error.HTTPError as err:
            if err.code == 404:
                print(f'{tarball_url} is missing. Skipping...')
                return False

            raise

        with open(self.tarball_path, 'wb') as f:
            f.write(bz2.decompress(req.read()))

        run_in_dir(
            ['tar', '-jxf', self.tarball_path],
            cwd=self.destdir())

        return True


def import_package(pkgname: str) -> Package:
    pkgmod = importlib.import_module(f'pybuild.packages.{pkgname}')
    for symbol_name in dir(pkgmod):
        symbol = getattr(pkgmod, symbol_name)
        if type(symbol) == type and symbol_name.lower() == pkgname:
            return symbol()

    # XXX: mypy asks for an explicit `return`. Is it necessary?
    return None


def enumerate_packages() -> Iterator[Package]:
    for child in (pathlib.Path(__file__).parent / 'packages').iterdir():
        pkgname, ext = os.path.splitext(os.path.basename(child))
        if ext != '.py':
            continue
        yield pkgname
