"""One fixed, table-free SQL probe through the official Seren psql command."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from uuid import UUID

from seren_harness.seren_cli import CliResult, SerenCli, OK, ERROR, TIMEOUT, UNAVAILABLE, DECODE_ERROR

SQL = ("SELECT pg_catalog.json_build_object('database', pg_catalog.current_database(), "
       "'answer', 6 * 7, 'read_only', pg_catalog.current_setting('transaction_read_only'));")


# The frozen database-read cases (docs/db-fixture). Their exact statements join the family
# only while the manifest still has this digest; any change removes them (fail closed).
DB_READ_MANIFEST = Path(__file__).resolve().parents[1] / "docs" / "db-fixture" / \
    "fixture-manifest.json"
DB_READ_MANIFEST_SHA256 = "218a20182411382b8735007677b02ea6a6298919140843a8dc1e407fd7bce14e"
OUTPUT_CAP = 65536  # bytes of psql output accepted as one result
DB_READ_CASES = ("case_open_invoices", "case_first_page_by_due_date", "case_missing_values")


def family():
    """Exact statements this probe may run: the constant query plus the frozen read cases."""
    statements = {SQL}
    try:
        raw = DB_READ_MANIFEST.read_bytes()
        if hashlib.sha256(raw).hexdigest() == DB_READ_MANIFEST_SHA256:
            frozen = json.loads(raw)["statements"]
            statements |= {frozen[k] for k in DB_READ_CASES}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return statements


def allowed(args):
    """Not a SQL parser: accept only an exact statement of the fixed family and an explicit
    target."""
    if not isinstance(args, list) or len(args) != 17 or not all(isinstance(a, str) for a in args):
        return False
    try:
        if any(str(UUID(args[i])) != args[i] for i in (2, 4)):
            return False
    except ValueError:
        return False
    return (re.fullmatch(r"[a-z][a-z0-9_]{0,62}", args[6]) is not None
            and args[16] in family()
            and args == probe_args(args[2], args[4], args[6], args[16]))


def probe_args(project, branch, database, sql=SQL):
    return ["psql", "--project-id", project, "--branch-id", branch,
            "--database", database, "--ssl", "require", "--", "-X", "-w", "-A", "-t",
            "--set=ON_ERROR_STOP=1", "-c", sql]


class SqlProbeCli(SerenCli):
    def json(self, *args):
        # Seren hands the connection URI directly to psql. Neither it nor raw stderr is
        # persisted here; display_argv contains only the fixed query and non-secret IDs.
        argv = ["-o", "json", *args]
        shown = self.display_argv(argv)
        self.history.append(shown)
        try:
            process = subprocess.Popen([self.executable, *argv], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env=self.env, start_new_session=True)
        except OSError:
            return CliResult(UNAVAILABLE, shown)
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except BaseException as error:
            # Killing only Seren would leave its psql child and DB connection behind.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            if isinstance(error, subprocess.TimeoutExpired):
                return CliResult(TIMEOUT, shown)
            raise
        if process.returncode:
            # Connection errors can contain opaque credentials outside URL syntax.
            # Keep a fixed diagnostic category, never the raw connection/error text.
            category = ("access or connection error" if re.search(
                r"unauthorized|forbidden|password|authentication|permission|connection", stderr, re.I)
                else "query or CLI error")
            return CliResult(ERROR, shown, exit_code=process.returncode, stderr_tail=category)
        if len(stdout) > OUTPUT_CAP:  # checked after capture; an oversized answer never passes
            return CliResult(DECODE_ERROR, shown, exit_code=0)
        try:
            data = json.loads(stdout)
        except ValueError:
            return CliResult(DECODE_ERROR, shown, exit_code=0)
        return CliResult(OK, shown, data, 0)


def environment(env, psql):
    # Exact psql binary checked by the caller. No inherited PG*, startup scripts,
    # password file, service file or interactive prompts. SSL is explicit in argv.
    return dict(env, PATH=str(Path(psql).parent) + ":/usr/bin:/bin",
                PGPASSFILE="/dev/null", PGSERVICEFILE="/dev/null", PGCONNECT_TIMEOUT="10",
                PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=10000")
