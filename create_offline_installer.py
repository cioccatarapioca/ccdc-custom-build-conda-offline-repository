"""Fetch the conda packages required to run the API and associated scripts and copy them to the installer directory.
This works by installing a temporary miniconda, downloading the required packages and all dependencies,
then copying the newly downloaded packages, which are those not already provided by the miniconda install.
"""
import glob
import os
import platform
import requests
import shutil
import subprocess
import sys
import tempfile
import re

IS_WINDOWS = sys.platform == 'win32'

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    if sys.version_info[0] >= 3:
        import winreg as reg
    else:
        import _winreg as reg

    HWND_BROADCAST = 0xffff
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    SendMessageTimeout = ctypes.windll.user32.SendMessageTimeoutW
    SendMessageTimeout.restype = None #wintypes.LRESULT
    SendMessageTimeout.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                wintypes.LPCWSTR, wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD)]

    def sz_expand(value, value_type):
        if value_type == reg.REG_EXPAND_SZ:
            return reg.ExpandEnvironmentStrings(value)
        else:
            return value

    def remove_from_system_path(pathname, allusers=True, path_env_var='PATH'):
        """Removes all entries from the path which match the value in 'pathname'

        You must call broadcast_environment_settings_change() after you are finished
        manipulating the environment with this and other functions.

        For example,
            # Remove Anaconda from PATH
            remove_from_system_path(r'C:\Anaconda')
            broadcast_environment_settings_change()
        """
        pathname = os.path.normcase(os.path.normpath(pathname))

        envkeys = [(reg.HKEY_CURRENT_USER, r'Environment')]
        if allusers:
            envkeys.append((reg.HKEY_LOCAL_MACHINE,
                r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'))
        for root, keyname in envkeys:
            key = reg.OpenKey(root, keyname, 0,
                    reg.KEY_QUERY_VALUE|reg.KEY_SET_VALUE)
            reg_value = None
            try:
                reg_value = reg.QueryValueEx(key, path_env_var)
            except WindowsError:
                # This will happen if we're a non-admin install and the user has
                # no PATH variable.
                reg.CloseKey(key)
                continue

            try:
                any_change = False
                results = []
                for v in reg_value[0].split(os.pathsep):
                    vexp = sz_expand(v, reg_value[1])
                    # Check if the expanded path matches the
                    # requested path in a normalized way
                    if os.path.normcase(os.path.normpath(vexp)) == pathname:
                        any_change = True
                    else:
                        # Append the original unexpanded version to the results
                        results.append(v)

                modified_path = os.pathsep.join(results)
                if any_change:
                    reg.SetValueEx(key, path_env_var, 0, reg_value[1], modified_path)
            except:
                # If there's an error (e.g. when there is no PATH for the current
                # user), continue on to try the next root/keyname pair
                reg.CloseKey(key)

    def add_to_system_path(paths, allusers=True, path_env_var='PATH'):
        """Adds the requested paths to the system PATH variable.

        You must call broadcast_environment_settings_change() after you are finished
        manipulating the environment with this and other functions.

        """
        # Make sure it's a list
        if not issubclass(type(paths), list):
            paths = [paths]

        # Ensure all the paths are valid before we start messing with the
        # registry.
        new_paths = None
        for p in paths:
            p = os.path.abspath(p)
            if not os.path.isdir(p):
                raise RuntimeError(
                    'Directory "%s" does not exist, '
                    'cannot add it to the path' % p
                )
            if new_paths:
                new_paths = new_paths + os.pathsep + p
            else:
                new_paths = p

        if allusers:
            # All Users
            root, keyname = (reg.HKEY_LOCAL_MACHINE,
                r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')
        else:
            # Just Me
            root, keyname = (reg.HKEY_CURRENT_USER, r'Environment')

        key = reg.OpenKey(root, keyname, 0,
                reg.KEY_QUERY_VALUE|reg.KEY_SET_VALUE)

        reg_type = None
        reg_value = None
        try:
            try:
                reg_value = reg.QueryValueEx(key, path_env_var)
            except WindowsError:
                # This will happen if we're a non-admin install and the user has
                # no PATH variable; in which case, we can write our new paths
                # directly.
                reg_type = reg.REG_EXPAND_SZ
                final_value = new_paths
            else:
                reg_type = reg_value[1]
                # If we're an admin install, put us at the end of PATH.  If we're
                # a user install, throw caution to the wind and put us at the
                # start.  (This ensures we're picked up as the default python out
                # of the box, regardless of whether or not the user has other
                # pythons lying around on their PATH, which would complicate
                # things.  It's also the same behavior used on *NIX.)
                if allusers:
                    final_value = reg_value[0] + os.pathsep + new_paths
                else:
                    final_value = new_paths + os.pathsep + reg_value[0]

            reg.SetValueEx(key, path_env_var, 0, reg_type, final_value)

        finally:
            reg.CloseKey(key)

    def broadcast_environment_settings_change():
        """Broadcasts to the system indicating that master environment variables have changed.

        This must be called after using the other functions in this module to
        manipulate environment variables.
        """
        SendMessageTimeout(HWND_BROADCAST, WM_SETTINGCHANGE, 0, u'Environment',
                    SMTO_ABORTIFHUNG, 5000, ctypes.pointer(wintypes.DWORD()))


class AnacondaMixin(object):
    """Run the Anaconda/Miniconda installer from Continuum Analytics.
    https://store.continuum.io/cshop/anaconda/

    This mixin class can be used to run the Anaconda or Miniconda installers
    in a given platform.

    The class inheriting from this mixin *must* have the following attributes:
    - installer: string representing the full path to the installer file
    - version: string representing the anaconda version number
    - distribution: string, either 'Anaconda' or 'Miniconda'
    - build_install_dir: string representing the path to the install destination
    """
    extensions = {'Windows': 'exe',
                  'Linux': 'sh',
                  'Darwin': 'sh'}

    platforms = {'Windows': 'Windows',
                 'Linux': 'Linux',
                 'Darwin': 'MacOSX'}

    architectures = {'64bit': 'x86_64'}

    system = platform.system()

    conda_python_version = '3'
    bitness = '64bit'

    @property
    def installer_name(self):
        # (Ana|Mini)conda-<VERSION>-<PLATFORM>-<ARCHITECTURE>.<EXTENSION>
        return '{0}{1}-{2}-{3}-{4}.{5}'.format(
            self.distribution,
            self.conda_python_version,
            self.version,
            AnacondaMixin.platforms[AnacondaMixin.system],
            AnacondaMixin.architectures[self.bitness],
            AnacondaMixin.extensions[AnacondaMixin.system])

    def base_conda_packages(self):
        pkgs = ['conda-build', 'conda-verify', 'jinja2']
        if IS_WINDOWS:
            pkgs += ['unxutils']
        return pkgs

    def pin_python_version(self):
        pin_file = os.path.join(self.build_install_dir, 'conda-meta', 'pinned')
        with open(pin_file, "w") as pinned:
            pin_string = "python {0}.*\n".format(self.conda_python_version)
            pinned.write(pin_string)

    def run(self):
        self.check_condarc_presence()
        self.install_anaconda()
        self.pin_python_version()
        self.conda_install(*self.base_conda_packages())

    def install_anaconda(self):
        print('Running %s' % self.install_args)
        outcome = subprocess.call(self.install_args)

        if IS_WINDOWS:
            self._clean_up_system_path()

        if outcome != 0:
            raise RuntimeError('Failed to run "{0}"'.format(self.install_args))

    @property
    def install_args(self):
        if IS_WINDOWS:
            install_args = [self.output_installer,
                            '/S',     # run install in batch mode (without manual intervention)
                            '/D=' + os.path.abspath(self.build_install_dir)]
        else:
            install_args = ['sh',
                            self.output_installer,
                            '-b',     # run install in batch mode (without manual intervention)
                            '-f',     # no error if install prefix already exists
                            '-p', os.path.abspath(self.build_install_dir)]
        return install_args

    def _clean_up_system_path(self):
        """The Windows installer modifies the PATH env var, so let's
        revert that using the same mechanism.
        """
        for_all_users = (not os.path.exists(
            os.path.join(self.build_install_dir, '.nonadmin')))

        remove_from_system_path(self.build_install_dir,
                                for_all_users,
                                'PATH')
        remove_from_system_path(os.path.join(self.build_install_dir, 'Scripts'),
                                for_all_users,
                                'PATH')
        broadcast_environment_settings_change()

    def conda_install(self, *package_specs):
        """Install a conda package given its specifications.
        E.g. self.conda_install('numpy==1.9.2', 'lxml')
        """
        self._run_pkg_manager('conda', ['install', '-y'], *package_specs)

    def _run_pkg_manager(self, pkg_manager_name, extra_args, *package_specs):
        my_env = os.environ.copy()
        # Set the condarc to the channels we want
        my_env["CONDARC"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'condarc-for-offline-installer-creation')
        # add Library\bin to path so that conda can find libcrypto
        if IS_WINDOWS:
            my_env['PATH'] = "%s;%s" % (os.path.join(self.build_install_dir, 'Library', 'bin'), my_env['PATH'])
        args = [self._args_for(pkg_manager_name)] + extra_args + list(package_specs)
        outcome = subprocess.call(args, env=my_env)
        if outcome != 0:
            print('_run_pkg_manager fail info')
            print(args)
            print(my_env)
            raise RuntimeError('Could not install {0} with {1}'.format(' '.join(package_specs), pkg_manager_name))

    def _args_for(self, executable_name):
        return os.path.join(self.build_install_dir,
                            ('Scripts' if IS_WINDOWS else 'bin'),
                            executable_name + ('.exe' if IS_WINDOWS else ''))

    def check_condarc_presence(self):
        for path in [
            '/etc/conda/.condarc',
            '/etc/conda/condarc',
            '/etc/conda/condarc.d/',
            '/var/lib/conda/.condarc',
            '/var/lib/conda/condarc',
            '/var/lib/conda/condarc.d/',
            '~/.conda/.condarc',
            '~/.conda/condarc',
            '~/.conda/condarc.d/',
            '~/.condarc',
            ]:
            if os.path.exists(os.path.expanduser(path)):
                print('Conda configuration found in %s. This might affect installation of packages' % path)

class MinicondaOfflineInstaller(AnacondaMixin):
    version = '4.7.12.1'
    ccdc_version = '-2'

    def __init__(self):
        self.distribution = 'Miniconda'
        self.output_installer = os.path.join(self.output_dir, self.installer_name)
        self.conda_bz2_src_packages = os.path.join(self.build_install_dir, 'pkgs', '*.bz2')
        self.conda_conda_src_packages = os.path.join(self.build_install_dir, 'pkgs', '*.conda')

    @property
    def name(self):
        return 'miniconda3'

    @property
    def build_install_dir(self):
        return 'build_temp'
    
    @property
    def output_dir(self):
        return os.path.join('output', self.name + '-' + self.version + self.ccdc_version)

    @property
    def output_conda_offline_channel(self):
        return os.path.join(self.output_dir, 'conda_offline_channel')

    def get_source_packages_from_mirror(self):
        installer_url='https://repo.continuum.io/miniconda/%s' % self.installer_name
        print("Get %s -> %s" % (installer_url, self.output_installer))
        r = requests.get(installer_url)
        with open(self.output_installer, 'wb') as fd:
            for chunk in r.iter_content(chunk_size=128):
                fd.write(chunk)

    def clean_build_and_output(self):
        try:
            shutil.rmtree(self.output_dir)
        except:
            pass
        try:
            shutil.rmtree(self.build_install_dir)
        except:
            pass

    def base_conda_packages(self):
        # these are the packages that we recommend for using the API https://downloads.ccdc.cam.ac.uk/documentation/API/installation_notes.html#using-conda
        api_pkgs = ['Pillow', 'six', 'lxml', 'numpy', 'matplotlib', 'pytest']
        # these packages are required by other scripts that we distribute
        script_pkgs = ['docxtpl', 'pockets', 'docutils', 'pygments', 'sphinx', 'pandas', 'py-xgboost']
        return api_pkgs + script_pkgs

    def conda_cleanup(self, *package_specs):
        """Remove package archives
        """
        self._run_pkg_manager('conda', ['clean', '-y', '--all'])

    def conda_update(self, *package_specs):
        """Remove package archives
        """
        self._run_pkg_manager('conda', ['update', '-y', '--all'])

    def conda_install(self, *package_specs):
        """Install a conda package given its specifications.
        E.g. self.conda_install('numpy==1.9.2', 'lxml')
        """
        self._run_pkg_manager('conda', ['install', '-y', '--download-only'], *package_specs)

    def package_name(self, package_filename):
        """Return the bit of a filename before the version number starts
        """
        return re.match(r"(.*)-\d.*", package_filename).group(1)

    def channel_arch(self):
        """return the conda channel architecture required for this build
        """
        if sys.platform == 'win32':
            if self.bitness == '64bit':
                return 'win-64'
            else:
                return 'win-32'
        elif sys.platform == 'darwin':
            return 'osx-64'
        else:
            return 'linux-64'

    def conda_index(self, channel):
        """index the conda channel directory
        """
        import pathlib
        patch_file = os.path.join( pathlib.Path(__file__).parent.absolute(), 'repodata-hotfixes/main.py')
        self._run_pkg_manager('conda', ['index', '-p', patch_file, channel])

    def copy_packages(self):
        """Copy packages from the miniconda install to the final installer location
           The copied packages are:
           Any conda packages which are newly downloaded.
        """
        conda_package_dest = os.path.join(self.output_conda_offline_channel, self.channel_arch())
        os.makedirs(conda_package_dest)

        known_packages = set()

        for conda_package in glob.glob(self.conda_bz2_src_packages):
            filename = os.path.basename(conda_package)
            known_packages.add(self.package_name(filename))
            shutil.copyfile(conda_package, os.path.join(conda_package_dest, filename))
        for conda_package in glob.glob(self.conda_conda_src_packages):
            filename = os.path.basename(conda_package)
            known_packages.add(self.package_name(filename))
            shutil.copyfile(conda_package, os.path.join(conda_package_dest, filename))

    def index_offline_channel(self):
        try:
            AnacondaMixin.conda_install(self, 'conda-build')
        except:
            pass # sorry, this reports an exception in miniconda2, but conda-build seems to be installed ok
        self.conda_index(self.output_conda_offline_channel)

    windows_install_script = """@echo off
if x%~s1==x (
  echo "install target_dir [ccdc_packages_and_package_name_pairs...]"
  goto end
)
setlocal
set installer_dir=%~dps0
start /wait "" "%installer_dir%{{ installer_exe }}" /AddToPath=0 /S /D=%~s1
call %~s1\\Scripts\\activate
call conda install -y --channel "%installer_dir%conda_offline_channel" --offline --override-channels {{ conda_packages }}
shift
:next_package
if not "%1" == "" (
    call conda install -y --channel "%installer_dir%%1_conda_channel" --offline --override-channels %2
    shift
    shift
    goto next_package
)
endlocal
:end
"""

    unix_install_script = """#!/bin/sh
if test $# -eq 0 ; then
    echo 'install target_dir [ccdc_packages_and_package_name_pairs...]'
    exit 1
fi
INSTALLER_DIR=$(dirname -- "$0")
chmod +x $INSTALLER_DIR/{{ installer_exe }}
$INSTALLER_DIR/{{ installer_exe }} -b -p $1
unset PYTHONPATH
unset PYTHONHOME
. $1/bin/activate ""
conda install -y --channel "$INSTALLER_DIR/conda_offline_channel" --offline --override-channels {{ conda_packages }}
shift
while test $# -gt 1
do
    conda install -y --channel "$INSTALLER_DIR/$1_conda_channel" --offline --override-channels $2
    shift
    shift
done
"""

    def write_install_script(self):
        installer_dir, installer_exe = os.path.split(self.output_installer)
        install_name = os.path.join(installer_dir, "install.{0}".format("bat" if sys.platform == 'win32' else "sh" ))
        if sys.platform == 'win32':
            script = self.windows_install_script
        else:
            script = self.unix_install_script
        script = script.replace('{{ installer_exe }}', installer_exe)
        script = script.replace('{{ conda_packages }}', ' '.join(self.base_conda_packages()))
        with open(install_name, "w") as f:
            f.write(script)
        if sys.platform != 'win32':
            os.chmod(install_name, 0o755)

    def build(self):
        print('Cleaning up build and output directories')
        self.clean_build_and_output()
        os.makedirs(self.build_install_dir)
        os.makedirs(self.output_dir)

        print('Getting installer')
        self.get_source_packages_from_mirror()

        print('Check there are no condarc files around')
        self.check_condarc_presence()

        print('Install anaconda in the build directory')
        self.install_anaconda()

        print('Pin python version')
        self.pin_python_version()

        print('Remove conda packages that were part of the installer')
        self.conda_cleanup()

        print('Download updates so that we can distribute them consistently')
        self.conda_update()

        print('Fetch packages')
        self.conda_install(*self.base_conda_packages())

        print('Copy packages to output directory')
        self.copy_packages()

        print('Create index of offline channel')
        self.index_offline_channel()

        print('Create install script')
        self.write_install_script()

if __name__ == '__main__':
    MinicondaOfflineInstaller().build()


