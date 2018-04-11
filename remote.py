import paramiko
import yaml
import shutil
import os
import time
from paramiko import SSHClient
import getpass
import tarfile
from scp import SCPClient
import socket
from parampy import StudyFile, ParamFile


#TODO: Refactor Remote to separate configuration-related stuff
SRC_DIR = os.path.dirname(os.path.realpath(__file__))
DEFAULTS_DIR = os.path.join(SRC_DIR, "defaults")
CONFIG_DIR = os.path.join(os.getenv("HOME"), ".parampy")
DEFAULT_DOWNLOAD_DIRS = ["output", "postproc"]

class Remote:
    def __init__(self, name="", remote_workdir=None, addr=None,\
                 port=22, username=None, key_login=False,shell="bash"):
        self.name = name
        self.remote_yaml = None
        self.key_login = key_login
        self.addr = addr
        self.port = port
        self.username = username
        self.remote_workdir = remote_workdir
        self.shell = shell
        self.ssh = SSHClient()
        self.ssh.load_system_host_keys()
        self.command_status = None
        self.scp = None


    @staticmethod
    def create_remote_template(path):
        filepath = os.path.join(DEFAULTS_DIR, "remote.yaml")
        try:
            if os.path.exists("remote.yaml"):
                raise Exception("Template 'remote.yaml' already exists.")
            else:
                shutil.copy(filepath, path)
        except Exception as error:
            raise Exception("Error:\n" + str(error))


    def _check_file(self):
        pass

    def load(self, path):
        with open(path, 'r') as remotefile:
            try:
                self.remote_yaml = yaml.load(remotefile)["remote"]
            except yaml.YAMLError as exc:
                print(exc)
        self._unpack_remote_yaml(self.remote_yaml)

    def _unpack_remote_yaml(self, yaml_remote):
        try:
            self.name = yaml_remote["name"]
            self.addr = yaml_remote["address"]
            self.port = yaml_remote["port"]
            self.remote_workdir = yaml_remote["remote-workdir"]
            self.username = yaml_remote["username"]
            self.resource_manager = yaml_remote["resource-manager"]
        except KeyError as e:
            raise Exception("Field %s not found in remote file." % str(e))
        # Optional params
        try:
            self.key_login = yaml_remote["key-login"]
        except Exception:
            pass


    def save(self, path):
        remotedata = {"remote": {}}
        try:
            remotedata["remote"]["name"] = self.name
            remotedata["remote"]["address"] = self.addr
            remotedata["remote"]["port"] = self.port
            remotedata["remote"]["remote-workdir"] = self.remote_workdir
            remotedata["remote"]["username"] = self.username
            remotedata["remote"]["shell"] = self.shell
        except Exception:
            raise Exception("Field %s not defined." % str(e))
        try:
            remotedata["remote"]["key-login"] = self.key_login
        except Exception:
            pass

        with open('%s.yaml', 'w') as remotefile:
            yaml.dump(remotedata, remotefile, default_flow_style=False)

    def available(self, timeout=5):
        try:
            self.connect(passwd="", timeout=5)
        except paramiko.AuthenticationException:
            return True
        except socket.timeout:
            return False
        else:
            return False

    def connect(self, passwd=None, timeout=None):
        if self.key_login:
            self.ssh.connect(self.addr, port=self.port, timeout=timeout)
        else:
            self.ssh.connect(self.addr, port=self.port, username=self.username,\
                             password=passwd, timeout=timeout)
        self.scp = SCPClient(self.ssh.get_transport())

    def command(self, cmd, timeout=None, close_on_error=True):
        stdin, stdout, stderr = self.ssh.exec_command(cmd, timeout=timeout)
        self.command_status = stdout.channel.recv_exit_status()
        error = stderr.readlines()
        if error:
            if close_on_error:
                self.close()
            raise Exception("".join([l for l in error if l]))
        return stdout.readlines()
    
    def upload(self, path_orig, path_dest):
        self.scp.put(path_orig, path_dest)

    def download(self, path_orig, path_dest):
        self.scp.get(path_orig, path_dest)

    def remote_file_exists(self, f):
        out = self.command("[ -f %s ]" % f)
        return not self.command_status

    def remote_dir_exists(self, d):
        out = self.command("[ -d %s ]" % d)
        return not self.command_status

    def close(self):
        if self.scp is not None:
            self.scp.close()
        self.ssh.close()

    def check_connection(self):
        pass

class RemoteDirExists(Exception):
    pass

class RemoteFileExists(Exception):
    pass


class StudyManager:
    def __init__(self, remote, study_path=None, case_path=None):
        assert study_path is not None or case_path is not None
        self.remote = remote
        if study_path is not None:
            self.study_path = os.path.abspath(study_path)
            self.study_file = StudyFile(path=self.study_path)
        else:
            self.study_path = None
        self.case_path = case_path
        self.case_name = None
        if case_path is not None:
            self.case_path = os.path.abspath(self.case_path)
            self.case_name = os.path.basename(self.case_path)
            self.study_path = os.path.dirname(self.case_path)
            self.study_name = os.path.basename(self.study_path)
            self.study_file = StudyFile(path=self.study_path)
            # Check if the cases.txt has been generated. Meaning it is a study.
            if not self.study_file.exists(self.study_path):
                self.study_name = "default"
        else:
            self.study_name = os.path.basename(self.study_path)
        self.tmp_dir = "/tmp"
        self.param_file = ParamFile()
     
    def _upload(self, name, path, keep_targz=False, force=False, remote_workdir=None):
        if remote_workdir is None:
            remote_workdir = self.remote.remote_workdir
        remotedir = os.path.join(remote_workdir, name)
        if self.remote.remote_dir_exists(remotedir):
            if not force:
                raise RemoteDirExists("")
        tar_name = self._compress(name, path)
        upload_src = os.path.join(self.tmp_dir, tar_name)
        upload_dest = remote_workdir
        self.remote.upload(upload_src, upload_dest)
        extract_src = os.path.join(upload_dest, tar_name)
        extract_dest = upload_dest
        out = self.remote.command("tar -xzf %s --directory %s --warning=no-timestamp" % (extract_src, extract_dest))
        os.remove(upload_src)
        if not keep_targz:
            out = self.remote.command("rm -f %s" % extract_src)

    def upload_case(self, keep_targz=False, force=False):
        # self._case_clean()
        remote_workdir = os.path.join(self.remote.remote_workdir, self.study_name)
        if not self.remote.remote_dir_exists(remote_workdir):
            out = self.remote.command("mkdir %s" % remote_workdir)
        try:
            self._upload(self.case_name, self.case_path, keep_targz, force, remote_workdir)
        except RemoteDirExists:
            raise RemoteDirExists("Case '%s' already exists in study '%s' in the remote '%s'." % (self.case_name,self.study_name, self.remote.name))


    def upload_study(self, keep_targz=False, force=False):
        try:
            self._upload(self.study_name, self.study_path, keep_targz, force)
        except RemoteDirExists:
            raise RemoteDirExists("Study '%s' already exists in remote '%s'." % (self.study_name, self.remote.name))

    def _compress(self, name, path):
        tar_name = name + ".tar.gz"
        with tarfile.open(os.path.join(self.tmp_dir, tar_name), "w:gz") as tar:
            tar.add(path, arcname=os.path.basename(path))
        return tar_name
        
    def submit_case(self):
        remote_workdir = os.path.join(self.remote.remote_workdir, self.study_name)
        remotedir = os.path.join(remote_workdir, self.case_name)
        if not self.remote.remote_dir_exists(remotedir):
            self.upload_case()
        try:
            time.sleep(1)
            self.remote.command("cd %s && qsub exec.sh" % remotedir)
        except Exception as error:
            if self.remote.command_status == 127:
                raise Exception("Command 'qsub' not found in remote '%s'." % self.remote.name)

    def submit_study(self):
        remote_studydir = os.path.join(self.remote.remote_workdir, self.study_name)
        if not self.remote.remote_dir_exists(remote_studydir):
            print "Uploading study '%s'..." % self.study_name
            self.upload_study()
        else:
            print "Study '%s' found in remote '%s'." % self.remote.name
        self.study_file.read()
        if self.study_file.is_empty():
            raise Exception("File 'cases.txt' is empty. Cannot submit case.")
        else:
            time.sleep(1)
            for case in self.study_file.cases:
                remote_casedir = os.path.join(remote_studydir, case[0])
                try:
                    time.sleep(0.1)
                    output = self.remote.command("cd %s && qsub exec.sh" % remote_casedir, timeout=10)
                    print output
                except Exception as error:
                    if self.remote.command_status == 127:
                        raise Exception("Command 'qsub' not found in remote '%s'." % self.remote.name)
                    else:
                        raise Exception(error)

    def download_study(self, force=False):
        remote_studydir = os.path.join(self.remote.remote_workdir, self.study_name)
        if not self.remote.remote_dir_exists(remote_studydir):
            raise Exception("Study does not exists in remote '%s'." % self.remote.name)
        self.study_file.read()
        if self.study_file.is_empty():
            raise Exception("File 'cases.txt' is empty. Cannot download case.")
        else:
            self.param_file.load(self.study_path)
            compress_dirs = ""
            for path in self.param_file["DOWNLOAD"]:
                include_list = []
                exclude_list = []
                path_name = path["path"]
                # TODO: Move checks of params.yaml to the Sections checkers in parampy.py
                include_exists = "include" in path
                exclude_exists = "exclude" in path
                path_wildcard = os.path.join("[0-9]*", path["path"])
                if exclude_exists and include_exists:
                    raise Exception("Both 'exclude' and 'include' defined for download path '%s'."\
                                    % path["path"])
                else:
                    if include_exists:
                        include_list = [os.path.join(path_wildcard, f) for f in path["include"]]
                        compress_dirs += " " + " ".join(include_list)
                    elif exclude_exists:
                        exclude_list = path["exclude"]
                        for f in exclude_list:
                            compress_dirs += " --exclude=%s" % f
                        compress_dirs += " " + path_wildcard
                    else:
                        compress_dirs += " " + path_wildcard

            compress_src = os.path.join(remote_studydir, self.study_name + ".tar.gz")
            tar_cmd = "tar -czf %s %s" % (compress_src, compress_dirs)
            force = True
            try:
                if force:
                    tar_cmd += " --ignore-failed-read"
                self.remote.command("cd %s && %s" % (remote_studydir, tar_cmd) ,\
                                    close_on_error=False, timeout=10)
            except Exception as error:
                if self.remote.command_status != 0:
                    self.remote.command("cd %s && rm -f %s" % (remote_studydir, compress_src), timeout=10)
                    raise Exception(error)
            self.remote.download(compress_src, self.study_path)
            self.remote.command("cd %s && rm -f %s" % (remote_studydir, compress_src), timeout=10)

    def status(self):
        try:
            time.sleep(0.1)
            self.remote.command("qstat", timeout=10)
        except Exception as error:
            if self.remote.command_status == 127:
                raise Exception("Command 'qstat' not found in remote '%s'." % self.remote.name)
            else:
                raise Exception(error)


            



if __name__ == "__main__":
    pass
