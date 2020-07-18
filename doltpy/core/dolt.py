from typing import List, Union, Mapping, Tuple
from datetime import datetime
from subprocess import Popen, PIPE, STDOUT
import os
from collections import OrderedDict
from retry import retry
from sqlalchemy.engine import Engine
from sqlalchemy import create_engine
import sqlalchemy
from doltpy.core.system_helpers import get_logger, SQL_LOG_FILE

logger = get_logger(__name__)

DEFAULT_HOST, DEFAULT_PORT = '127.0.0.1', 3306


class DoltException(Exception):

    """
    A class representing a Dolt exception.
    """
    def __init__(self, exec_args, stdout, stderr, exitcode):
        self.exec_args = exec_args
        self.stdout = stdout
        self.stderr = stderr
        self.exitcode = exitcode


class DoltServerNotRunningException(Exception):

    def __init__(self, message):
        self.message = message


class DoltWrongServerException(Exception):

    def __init__(self, message):
        self.message = message


def _execute(args: List[str], cwd: str):
    _args = ['dolt'] + args
    proc = Popen(args=_args, cwd=cwd, stdout=PIPE, stderr=PIPE)
    out, err = proc.communicate()
    exitcode = proc.returncode

    if exitcode != 0:
        raise DoltException(_args, out, err, exitcode)

    return out.decode('utf-8')


class DoltStatus:
    """
    Represents the current status of a Dolt repo, summarized by the is_clean field which is True if the wokring set is
    clean, and false otherwise. If the working set is not clean, then the changes are stored in maps, one for added
    tables, and one for modifications, each name maps to a flag indicating whether the change is staged.
    """
    def __init__(self, is_clean: bool, modified_tables: Mapping[str, bool], added_tables: Mapping[str, bool]):
        self.is_clean = is_clean
        self.modified_tables = modified_tables
        self.added_tables = added_tables


class DoltTable:
    """
    Represents a Dolt table in the working set.
    """
    def __init__(self, name: str, table_hash: str = None, rows: int = None, system: bool = False):
        self.name = name
        self.table_hash = table_hash
        self.rows = rows
        self.system = system


class DoltCommit:
    """
    Represents metadata about a commit, including a ref, timestamp, and author, to make it easier to sort and present
    to the user.
    """
    def __init__(self, ref: str, ts: datetime, author: str):
        self.hash = ref
        self.ts = ts
        self.author = author

    def __str__(self):
        return '{}: {} @ {}'.format(self.hash, self.author, self.ts)


class DoltKeyPair:
    """
    Represents a key pair generated by Dolt for authentication with remotes.
    """
    def __init__(self, public_key: str, key_id: str, active: bool):
        self.public_key = public_key
        self.key_id = key_id
        self.active = active


class DoltBranch:
    """
    Represents a branch, along with the commit it points to.
    """
    def __init__(self, name: str, commit_id: str):
        self.name = name
        self.commit_id = commit_id


class DoltRemote:
    """
    Represents a remote, effecitvely a name and URL pair.
    """
    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url


class Dolt:
    """
    This class wraps the Dolt command line interface, mimicking functionality exactly to the extent that is possible.
    Some commands simply do not translate to Python, such as `dolt sql` (with no arguments) since that command
    launches an interactive shell.
    """

    def __init__(self, repo_dir: str):
        self._repo_dir = repo_dir
        self.server = None

    def repo_dir(self):
        """
        The absolute path of the directory this repository represents.
        :return:
        """
        return self._repo_dir

    def execute(self, args: List[str], print_output: bool = True, restart_server: bool = False) -> List[str]:
        """
        Manages executing a dolt command, pass all commands, sub-commands, and arguments as they would appear on the
        command line.
        :param args:
        :param print_output:
        :param restart_server:
        :return:
        """
        was_serving = False
        if restart_server and self.server is not None:
            was_serving = True
            self.sql_server_stop()

        output = _execute(args, self.repo_dir())

        if print_output:
            logger.info(output)

        @retry(exceptions=Exception, delay=2, tries=10)
        def verify_connection():
            engine = self.get_engine()
            with engine.connect() as _:
                logger.info('Verified database server running')

        if was_serving:
            # TODO:
            #   this is a a problem because we restart with different parameters, solution is to
            #   to store a config object on the repo
            self.sql_server()
            verify_connection()

        return output.split('\n')

    @staticmethod
    def init(repo_dir: str = None) -> 'Dolt':
        """
        Creates a new repository in the directory specified, creating the directory if `create_dir` is passed, and returns
        a `Dolt` object representing the newly created repo.
        :return:
        """
        if not repo_dir:
            repo_dir = os.getcwd()

        if os.path.exists(repo_dir):
            logger.info('Initializing Dolt repo in existing dir {}'.format(repo_dir))
        else:
            try:
                logger.info('Creating directory {}'.format(repo_dir))
            except Exception as e:
                raise e

        _execute(['init'], cwd=repo_dir)
        return Dolt(repo_dir)

    def status(self) -> DoltStatus:
        """
        Parses the status of this repository into a `DoltStatus` object.
        :return:
        """
        new_tables, changes = {}, {}

        output = self.execute(['status'], print_output=False)

        if 'clean' in str('\n'.join(output)):
            return DoltStatus(True, changes, new_tables)
        else:
            staged = False
            for line in output:
                _line = line.lstrip()
                if _line.startswith('Changes to be committed'):
                    staged = True
                elif _line.startswith('Changes not staged for commit'):
                    staged = False
                elif _line.startswith('Untracked files'):
                    staged = False
                elif _line.startswith('modified'):
                    changes[_line.split(':')[1].lstrip()] = staged
                elif _line.startswith('new table'):
                    new_tables[_line.split(':')[1].lstrip()] = staged
                else:
                    pass

        return DoltStatus(False, changes, new_tables)

    def add(self, table_or_tables: Union[str, List[str]]):
        """
        Adds the table or list of tables in the working tree to staging.
        :param table_or_tables:
        :return:
        """
        if type(table_or_tables) == str:
            to_add = [table_or_tables]
        else:
            to_add = table_or_tables
        self.execute(["add"] + to_add, restart_server=True)
        return self.status()

    def reset(self, table_or_tables: Union[str, List[str]], hard: bool = False, soft: bool = False):
        """
        Reset a table or set of tables that have changes in the working set to their value at the tip of the current
        branch.
        :param table_or_tables:
        :param hard:
        :param soft:
        :return:
        """
        if type(table_or_tables) == str:
            to_reset = [table_or_tables]
        else:
            to_reset = table_or_tables

        args = ['reset']

        assert not(hard and soft), 'Cannot reset hard and soft'

        if hard:
            args.append('--hard')
        if soft:
            args.append('--soft')

        self.execute(args + to_reset)

    def commit(self, message: str = None, allow_empty: bool = False, date: datetime = None):
        """
        Create a commit with the currents in the working set that are currently in staging.
        :param message:
        :param allow_empty:
        :param date:
        :return:
        """
        args = ['commit', '-m', message]

        if allow_empty:
            args.append('--allow-empty')

        if date:
            # TODO format properly
            args.extend(['--date', str(date)])

        self.execute(args, restart_server=True)

    def sql(self,
            query: str = None,
            result_format: str = None,
            execute: str = False,
            save: str = None,
            message: str = None,
            list_saved: bool = False,
            batch: bool = False,
            multi_db_dir: str = None):
        """
        Execute a SQL query, using the options to dictate how it is executed, and where the output goes.
        :param query: query to be executed
        :param result_format: the file format of the
        :param execute: execute a saved query, not valid with other parameters
        :param save: use the name provided to save the value of query
        :param message: the message associated with the saved query, if any
        :param list_saved: print out a list of saved queries
        :param batch: execute in batch mode, one statement after the other delimited by ;
        :param multi_db_dir: use a directory of Dolt repos, each one treated as a database
        :return:
        """
        args = ['sql']

        if list_saved:
            assert not any([query, result_format, save, message, batch, multi_db_dir])
            args.append('--list-saved')
            self.execute(args)

        if execute:
            assert not any([query, save, message, list_saved, batch, multi_db_dir])
            args.extend(['--execute', execute])

        if multi_db_dir:
            args.extend(['--multi-db-dir', multi_db_dir])

        if batch:
            args.append('--batch')

        if save:
            args.extend(['--save', save])
            if message:
                args.extend(['--message', message])

        args.extend(['--query', query])
        self.execute(args)

    def sql_server(self,
                   config: str = None,
                   host: str = None,
                   port: int = None,
                   user: str = None,
                   password: str = None,
                   timeout: int = None,
                   readonly: bool = False,
                   loglevel: str = 'info',
                   multi_db_dir: str = None,
                   no_auto_commit: str = None):
        """
        Start a MySQL Server process on local host using the parameters to configure behavior. The parameters are
        self-explanatory, but the config is a way to provide them as a YAML file rather than as function
        arguments.
        :param config:
        :param host:
        :param port:
        :param user:
        :param password:
        :param timeout:
        :param readonly:
        :param loglevel:
        :param multi_db_dir:
        :param no_auto_commit:
        :return:
        """
        def start_server(server_args):
            if self.server is not None:
                logger.warning('Server already running')

            log_file = SQL_LOG_FILE or os.path.join(self.repo_dir(), 'mysql_server.log')

            proc = Popen(args=['dolt'] + server_args,
                         cwd=self.repo_dir(),
                         stdout=open(log_file, 'w'),
                         stderr=STDOUT)

            self.server = proc

        args = ['sql-server']

        if config:
            args.extend(['--config', config])
        else:
            if host:
                args.extend(['--host', host])
            if port:
                args.extend(['--port', str(port)])
            if user:
                args.extend(['--user', user])
            if password:
                args.extend(['--password', password])
            if timeout:
                args.extend(['--timeout', int(timeout)])
            if readonly:
                args.extend(['--readonly'])
            if loglevel:
                args.extend(['--loglevel', loglevel])
            if multi_db_dir:
                args.extend(['--multi-db-dir', multi_db_dir])
            if no_auto_commit:
                args.extend(['--no-auto-commit', no_auto_commit])

        start_server(args)

    @property
    def repo_name(self):
        return str(self.repo_dir()).split('/')[-1].replace('-', '_')

    def get_engine(self, host: str = None, port: int = None) -> Engine:
        """
        Get a connection to ths server process that this repo is running, raise an exception if it is not running.
        :param host:
        :param port:
        :return:
        """
        database = self.repo_name
        host = host or DEFAULT_HOST
        port = port or DEFAULT_PORT

        logger.info('Attempting to connect to Dolt MySQL Server instance running on {}:{}'.format(host, port))

        def inner():
            return create_engine('mysql+mysqlconnector://{user}@{host}:{port}/{database}'.format(user='root',
                                                                                                 host=host,
                                                                                                 port=port,
                                                                                                 database=database), echo=True)

        return inner()

    def sql_server_stop(self):
        """
        Stop the MySQL Server process this repo is running.
        :return:
        """
        if self.server is None:
            logger.warning("Server is not running")
            return

        self.server.kill()
        self.server = None

    def log(self, number: int = None, commit: str = None) -> OrderedDict:
        """
        Parses the log created by running the log command into instances of `DoltCommit` that provide detail of the
        commit, including timestamp and hash.
        :param number:
        :param commit:
        :return:
        """
        args = ['log']

        if number:
            args.extend(['--number', number])
        if commit:
            raise NotImplementedError()

        output = self.execute(args, print_output=False)
        current_commit, author, date = None, None, None
        result = OrderedDict()
        for line in output:
            if line.startswith('commit'):
                current_commit = line.split(' ')[1]
            elif line.startswith('Author'):
                author = line.split(':')[1].lstrip()
            elif line.startswith('Date'):
                date = datetime.strptime(line.split(':', maxsplit=1)[1].lstrip(), '%a %b %d %H:%M:%S %z %Y')
            elif current_commit is not None:
                assert current_commit is not None and date is not None and author is not None
                result[current_commit] = DoltCommit(current_commit, date, author)
                current_commit = None
            else:
                pass

        return result

    def diff(self,
             commit: str = None,
             other_commit: str = None,
             table_or_tables: Union[str, List[str]] = None,
             data: bool = False,
             schema: bool = False, # can we even support this?
             summary: bool = False,
             sql: bool = False,
             where: str = None,
             limit: int = None):
        """
        Executes a diff command and prints the output. In the future we plan to create a diff object that will allow
        for programmatic interactions.
        :param commit: commit to diff against the tip of the current branch
        :param other_commit: optionally specify two specific commits if desired
        :param table_or_tables: table or list of tables to diff
        :param data: diff only data
        :param schema: diff only schema
        :param summary: summarize the data changes shown, valid only with data
        :param sql: show the diff in terms of SQL
        :param where: apply a where clause to data diffs
        :param limit: limit the number of rows shown in a data diff
        :return:
        """
        switch_count = [el for el in [data, schema, summary] if el]
        assert len(switch_count) <= 1, 'At most one of delete, copy, move can be set to True'

        if type(table_or_tables) == str:
            tables = [table_or_tables]
        else:
            tables = table_or_tables

        args = ['diff']

        if data:
            if where:
                args.extend(['--where', where])
            if limit:
                args.extend(['--limit', limit])

        if summary:
            args.append('--summary')

        if schema:
            args.extend('--schema')

        if sql:
            args.append('--sql')

        if commit:
            args.append(commit)
        if other_commit:
            args.append(other_commit)

        if tables:
            args.append(' '.join(tables))

        self.execute(args)

    def blame(self, table_name: str, rev: str = None):
        """
        Executes a blame command that prints out a table that shows the authorship of the last change to a row.
        :param table_name:
        :param rev:
        :return:
        """
        args = ['blame']

        if rev:
            args.append(rev)

        args.append(table_name)
        self.execute(args)

    def branch(self,
               branch_name: str = None,
               start_point: str = None,
               new_branch: str = None,
               force: bool = False,
               delete: bool = False,
               copy: bool = False,
               move: bool = False):
        """
        Checkout, create, delete, move, or copy, a branch. Only
        :param branch_name:
        :param start_point:
        :param new_branch:
        :param force:
        :param delete:
        :param copy:
        :param move:
        :return:
        """
        switch_count = [el for el in [delete, copy, move] if el]
        assert len(switch_count) <= 1, 'At most one of delete, copy, move can be set to True'

        if not any([branch_name, delete, copy, move]):
            assert not force, 'force is not valid without providing a new branch name, or copy, move, or delete being true'
            return self._get_branches()

        args = ['branch']
        if force:
            args.append('--force')

        if branch_name and not(delete and copy and move):
            args.append(branch_name)
            if start_point:
                args.append(start_point)
            _execute(args, self.repo_dir())
            return self._get_branches()

        if copy:
            assert new_branch, 'must provide new_branch when copying a branch'
            args.append('--copy')
            if branch_name:
                args.append(branch_name)
            args.extend(new_branch)
            self.execute(args)

        if delete:
            assert branch_name, 'must provide branch_name when deleting'
            args.extend(['--delete', branch_name])
            self.execute(args)

        if move:
            assert new_branch, 'must provide new_branch when moving a branch'
            args.append('--move')
            if branch_name:
                args.append(branch_name)
            args.extend(new_branch)
            self.execute(args)

        if branch_name:
            args.extend(branch_name)
            if start_point:
                args.append(start_point)
            self.execute(args)

        return self._get_branches()

    def _get_branches(self) -> Tuple[DoltBranch, List[DoltBranch]]:
        args = ['branch', '--list', '--verbose']
        output = self.execute(args)
        branches, active_branch = [], None
        for line in output:
            if not line:
                break
            elif line.startswith('*'):
                split = line.lstrip()[1:].split()
                branch, commit = split[0], split[1]
                active_branch = DoltBranch(branch, commit)
                branches.append(active_branch)
            else:
                split = line.lstrip().split()
                branch, commit = split[0], split[1]
                branches.append(DoltBranch(branch, commit))

        return active_branch, branches

    def checkout(self,
                 branch: str = None,
                 table_or_tables: Union[str, List[str]] = None,
                 checkout_branch: bool = False,
                 start_point: str = None):
        """
        Checkout an existing branch, or create a new one, optionally at a specified commit. Or, checkout a table or list
        of tables.
        :param branch: branch to checkout or create
        :param table_or_tables: table or tables to checkout
        :param checkout_branch: branch to checkout
        :param start_point: tip of new branch
        :return:
        """
        args = ['checkout']

        if type(table_or_tables) == str:
            tables = [table_or_tables]
        else:
            tables = table_or_tables

        if branch:
            assert not table_or_tables, 'No table_or_tables '
            if checkout_branch:
                args.append('-b')
                if start_point:
                    args.append(start_point)
            args.append(branch)

        if tables:
            assert not branch, 'Passing a branch not compatible with tables'
            args.append(' '.join(tables))

        self.execute(args, restart_server=True)

    def remote(self, add: bool = False, name: str = None, url: str = None, remove: bool = None):
        """
        Add or remove remotes to this repository. Note we do not currently support some more esoteric options for using
        AWS and GCP backends, but will do so in a future release.
        :param add:
        :param name:
        :param url:
        :param remove:
        :return:
        """
        args = ['remote', '--verbose']

        if not(add or remove):
            output = self.execute(args, print_output=False)

            remotes = []
            for line in output:
                if not line:
                    break

                split = line.lstrip().split()
                remotes.append(DoltRemote(split[0], split[1]))

            return remotes

        if remove:
            assert not add, 'add and remove are not comptaibe '
            assert name, 'Must provide the name of a remote to move'
            args.extend(['remove', name])

        if add:
            assert name and url, 'Must provide name and url to add'
            args.extend(['add', name, url])

        self.execute(args)

    def push(self, remote: str, refspec: str = None, set_upstream: str = None, force: bool = False):
        """
        Push the to the specified remote. If set_upstream is provided will create an upstream reference of all branches
        in a repo.
        :param remote:
        :param refspec: optionally specify a branch to push
        :param set_upstream: add upstream reference for every branch successfully pushed
        :param force: overwrite the history of the upstream with this repo's history
        :return:
        """
        args = ['push']

        if set_upstream:
            args.append('--set-upstream')

        if force:
            args.append('--force')

        args.append(remote)
        if refspec:
            args.append(refspec)

        # just print the output
        self.execute(args, restart_server=True)

    def pull(self, remote: str):
        """
        Pull the latest changes from the specified remote.
        :param remote:
        :return:
        """
        self.execute(['pull', remote], restart_server=True)

    def fetch(self, remote: str = 'origin', refspec_or_refspecs: Union[str, List[str]] = None, force: bool = False):
        """
        Fetch the specified branch or list of branches from the remote provided, defaults to origin.
        :param remote: the reomte to fetch from
        :param refspec_or_refspecs: branch or branches to fetch
        :param force: whether to override local history with remote
        :return:
        """
        args = ['fetch']

        if type(refspec_or_refspecs) == str:
            refspecs = [refspec_or_refspecs]
        else:
            refspecs = refspec_or_refspecs

        if force:
            args.append('--force')
        if remote:
            args.append(remote)
        if refspec_or_refspecs:
            args.extend(refspecs)

        self.execute(args)

    @staticmethod
    def clone(remote_url: str, new_dir: str = None, remote: str = None, branch: str = None):
        """
        Clones a repository into the repository specified, currently only supports DoltHub as a remote.
        :param remote_url:
        :param new_dir:
        :param remote:
        :param branch:
        :return:
        """
        args = ["clone", remote_url]

        if remote:
            args.extend(['--remote', remote])

        if branch:
            args.extend(['--branch', branch])

        if not new_dir:
            split = remote_url.split('/')
            new_dir = os.path.join(os.getcwd(), split[-1])
            os.mkdir(new_dir)

        if new_dir:
            args.append(new_dir)

        _execute(args, cwd=new_dir)

        return Dolt(new_dir)

    def creds_new(self) -> bool:
        """
        Create a new set of credentials for this Dolt repository.
        :return:
        """
        args = ['creds', 'new']

        output = self.execute(args, print_output=False)

        if len(output) == 2:
            for out in output:
                logger.info(out)
        else:
            raise ValueError('Unexpected output: \n{}'.format('\n'.join(output)))

        return True

    def creds_rm(self, public_key: str) -> bool:
        """
        Remove the key pair identified by the specified public key ID.
        :param public_key:
        :return:
        """
        args = ['creds', 'rm', public_key]

        output = self.execute(args, print_output=False)

        if output[0].startswith('failed'):
            logger.error(output[0])
            raise DoltException('Tried to remove non-existent creds')

        return True

    def creds_ls(self) -> List[DoltKeyPair]:
        """
        Parse the set of keys this repo has into `DoltKeyPair` objects.
        :return:
        """
        args = ['creds', 'ls', '--verbose']

        output = _execute(args, self.repo_dir())

        creds = []
        for line in output:
            if line.startswith('*'):
                active = True
                split = line[1:].lstrip().split(' ')
            else:
                active = False
                split = line.lstrip().splity(' ')

            creds.append(DoltKeyPair(split[0], split[1], active))

        return creds

    def creds_check(self, endpoint: str = None, creds: str = None) -> bool:
        """
        Check that credentials authenticate with the specified endpoint, return True if authorized, False otherwise.
        :param endpoint: the endpoint to check
        :param creds: creds identified by public key ID
        :return:
        """
        args = ['dolt', 'creds', 'check']

        if endpoint:
            args.extend(['--endpoint', endpoint])
        if creds:
            args.extend(['--creds', creds])

        output = _execute(args, self.repo_dir())

        if output[3].startswith('error'):
            logger.error('\n'.join(output[3:]))
            return False

        return True

    def creds_use(self, public_key_id: str) -> bool:
        """
        Use the credentials specified by the provided public keys ID.
        :param public_key_id:
        :return:
        """
        args = ['creds', 'use', public_key_id]

        output = _execute(args, self.repo_dir())

        if output[0].startswith('error'):
            logger.error('\n'.join(output[3:]))
            raise DoltException('Bad public key')

        return True

    def creds_import(self, jwk_filename: str, no_profile: str):
        """
        Not currently supported.
        :param jwk_filename:
        :param no_profile:
        :return:
        """
        raise NotImplementedError()

    def config(self,
               name: str = None,
               value: str = None,
               add: bool = False,
               list: bool = False,
               get: bool = False,
               unset: bool = False):
        """
        Manipulate the global and local configs by examining and updating config values. This passes to Dolt which then
        manipulates its JSON config files.

        In a future version we will use an object interface to represent the current state of the configs.
        :param name:
        :param value:
        :param add:
        :param list:
        :param get:
        :param unset:
        :return:
        """
        switch_count = [el for el in [add, list, get, unset] if el]
        assert len(switch_count) == 1, 'Exactly one of add, list, get, unset must be True'

        args = ['config']

        if add:
            assert name and value, 'For add, name and value must be set'
            args.extend(['--add', '--name', name, '--value', value])
        if list:
            assert not(name or value), 'For list, no name and value provided'
            args.append('--list')
        if get:
            assert name and not value, 'For get, only name is provided'
            args.extend(['--get', '--name', name])
        if unset:
            assert name and not value, 'For get, only name is provided'
            args.extend(['--unset', '--name', name])

        self.execute(args, self.repo_dir()).split('\n')

    def ls(self, system: bool = False, all: bool = False) -> List[DoltTable]:
        """
        List the tables in the working set, the system tables, or all. Parses the tables and their object hash into an
        object that also provides row count.
        :param system:
        :param all:
        :return:
        """
        args = ['ls', '--verbose']

        if all:
            args.append('--all')

        if system:
            args.append('--system')

        output = self.execute(args, print_output=False)
        tables = []
        system_pos = None

        for i, line in enumerate(output):
            if line.startswith('Tables') or not line:
                pass
            elif line.startswith('System'):
                system_pos = i
                break
            else:
                if not line:
                    pass
                split = line.lstrip().split()
                tables.append(DoltTable(split[0], split[1], split[2]))

        if system_pos:
            for line in output[system_pos:]:
                if line.startswith('System'):
                    pass
                else:
                    tables.append(DoltTable(line.strip(), system=True))

        return tables

    def schema_export(self, table: str, filename: str = None):
        """
        Export the scehma of the table specified to the file path specified.
        :param table:
        :param filename:
        :return:
        """
        args = ['schema', 'export', table]

        if filename:
            args.extend(['--filename', filename])
            _execute(args, self.repo_dir())
            return True
        else:
            output = _execute(args, self.repo_dir())
            logger.info('\n'.join(output))
            return True

    def schema_import(self,
                      table: str,
                      filename: str,
                      create: bool = False,
                      update: bool = False,
                      replace: bool = False,
                      dry_run: bool = False,
                      keep_types: bool = False,
                      file_type: bool = False,
                      pks: List[str] = None,
                      map: str = None,
                      float_threshold: float = None,
                      delim: str = None):
        """
        This implements schema import from Dolt, it works by inferring a schema from the file provided. It operates in
        three modes: create, update, and replace. All require a table name. Create and replace require a primary key, as
        they replace an existing table with a new one with a newly inferred schema.

        :param table: name of the table to create or update
        :param filename: file to infer schema from
        :param create: create a table
        :param update: update a table
        :param replace: replace a table
        :param dry_run: output the SQL to run, do not execute it
        :param keep_types: when a column already exists, use its current type
        :param file_type: type of file used for schema inference
        :param pks: the list of primary keys
        :param map: mapping file mapping column name to new value
        :param float_threshold: minimum value fractional component must have to be float
        :param delim: the delimeter used in the file being inferred from
        :return:
        """
        switch_count = [el for el in [create, update, replace] if el]
        assert len(switch_count) == 1, 'Exactly one of create, update, replace must be True'

        args = ['schema', 'import']

        if create:
            args.append('--create')
            assert pks, 'When create is set to True, pks must be provided'
        if update:
            args.append('--update')
        if replace:
            args.append('--replace')
            assert pks, 'When replace is set to True, pks must be provided'
        if dry_run:
            args.append('--dry-run')
        if keep_types:
            args.append('--keep-types')
        if file_type:
            args.extend(['--file_type', file_type])
        if pks:
            args.extend(['--pks', ','.join(pks)])
        if map:
            args.extend(['--map', map])
        if float_threshold:
            args.extend(['--float-threshold', float_threshold])
        if delim:
            args.extend(['--delim', delim])

        args.extend([table, filename])

        self.execute(args)

    def schema_show(self, table_or_tables: Union[str, List[str]], commit: str = None):
        """
        Dislay the schema of the specified table or tables at the (optionally) specified commit, defaulting to the tip
        of master on the current branch.
        :param table_or_tables:
        :param commit:
        :return:
        """
        if type(table_or_tables) == str:
            to_show = [table_or_tables]
        else:
            to_show = table_or_tables

        args = ['schema', 'show']

        if commit:
            args.append(commit)

        args.extend(to_show)

        self.execute(args)

    def table_rm(self, table_or_tables: Union[str, List[str]]):
        """
        Remove the table or list of tables provided from the working set.
        :param table_or_tables:
        :return:
        """
        if type(table_or_tables) == str:
            tables = [table_or_tables]
        else:
            tables = table_or_tables

        self.execute(['rm', ' '.join(tables)])

    def table_import(self,
                     table: str,
                     filename: str,
                     create_table: bool = False,
                     update_table: bool = False,
                     force: bool = False,
                     mapping_file: str = None,
                     pk: List[str] = None,
                     replace_table: bool = False,
                     file_type: bool = None,
                     continue_importing: bool = False,
                     delim: bool = None):
        """
        Import a table from a filename, inferring the schema from the file. Operates in two possible modes, update,
        create, or replace. If creating must provide a primary key.
        :param table: the table to be created or updated
        :param filename: the data file to import
        :param create_table: create a table
        :param update_table: update a table
        :param force: force the import to overwrite existing data
        :param mapping_file: file mapping column names in file to new names
        :param pk: columns from which to build a primary key
        :param replace_table: replace existing tables
        :param file_type: the type of the file being imported
        :param continue_importing:
        :param delim:
        :return:
        """
        switch_count = [el for el in [create_table, update_table, replace_table] if el]
        assert len(switch_count) == 1, 'Exactly one of create, update, replace must be True'

        args = ['table', 'import']

        if create_table:
            args.append('--create')
            assert pk, 'When create is set to True, pks must be provided'
        if update_table:
            args.append('--update')
        if replace_table:
            args.append('--replace')
            assert pk, 'When replace is set to True, pks must be provided'
        if file_type:
            args.extend(['--file_type', file_type])
        if pk:
            args.extend(['--pks', ','.join(pk)])
        if mapping_file:
            args.extend(['--map', mapping_file])
        if delim:
            args.extend(['--delim', delim])
        if continue_importing:
            args.append('--continue')
        if force:
            args.append('--force')

        args.extend([table, filename])
        self.execute(args)

    def table_export(self,
                     table: str,
                     filename: str,
                     force: bool = False,
                     schema: str = None,
                     mapping_file: str = None,
                     pk: List[str] = None,
                     file_type: str = None,
                     continue_exporting: bool = False):
        """

        :param table:
        :param filename:
        :param force:
        :param schema:
        :param mapping_file:
        :param pk:
        :param file_type:
        :param continue_exporting:
        :return:
        """
        args = ['table', 'export']

        if force:
            args.append('--force')

        if continue_exporting:
            args.append('--continue')

        if schema:
            args.extend(['--schema', schema])

        if mapping_file:
            args.extend(['--map', mapping_file])

        if pk:
            args.extend(['--pk', ','.join(pk)])

        if file_type:
            args.extend(['--file-type', file_type])

        args.extend([table, filename])
        self.execute(args)

    def table_mv(self, old_table: str, new_table: str, force: bool = False):
        """
        Rename a table from name old_table to name new_table.
        :param old_table: existing table
        :param new_table: new table name
        :param force: override changes in the working set
        :return:
        """
        args = ['table', 'mv']

        if force:
            args.append('--force')

        args.extend([old_table, new_table])
        self.execute(args)

    def table_cp(self, old_table: str, new_table: str, commit: str = None, force: bool = False):
        """
        Copy an existing table to a new table, optionally at a specified commit.
        :param old_table: existing table name
        :param new_table: new table name
        :param commit: commit at which to read old_table
        :param force: override changes in the working set
        :return:
        """
        args = ['table', 'cp']

        if force:
            args.append('--force')

        if commit:
            args.append(commit)

        args.extend([old_table, new_table])
        self.execute(args)
