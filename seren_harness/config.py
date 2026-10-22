"""Load and check the operator configuration. Secrets are referenced, never stored."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from seren_harness.models import LIVE, SIMULATION

SENDERS = ("unavailable", "human", "simulation")
OBSERVERS = ("unavailable", "discord_rest", "simulation")
_SECRET_KEY = re.compile(r"(?i)(token|secret|password|api[_-]?key|credential)")
_SECRET_REFERENCE = re.compile(r"_env$")
# The pinned CLI only trims a profile name and joins it as a path component
# (cli/src/config.rs resolve_profile / profile_dir); the harness accepts one safe component.
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class ConfigError(Exception):
    pass


def _reject_secret_values(value, path="config"):
    if isinstance(value, dict):
        for key, item in value.items():
            where = path + "." + key
            if _SECRET_KEY.search(key) and not _SECRET_REFERENCE.search(key):
                raise ConfigError(where + " looks like a secret value; reference an environment "
                                  "variable with a key ending in _env instead")
            _reject_secret_values(item, where)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_values(item, path + "[" + str(index) + "]")


def _require(mapping, key, where, kind=str):
    if key not in mapping or not isinstance(mapping[key], kind) or mapping[key] in ("", None):
        raise ConfigError(where + "." + key + " is required")
    return mapping[key]


def _positive(mapping, key, default, where):
    value = mapping.get(key, default)
    if type(value) not in (int, float) or value <= 0:
        raise ConfigError(where + "." + key + " must be a positive number")
    return value


class Config:
    def __init__(self, data, source=None):
        if not isinstance(data, dict):
            raise ConfigError("configuration must be a JSON object")
        _reject_secret_values(data)
        self.data = data
        self.source = source
        base = Path(source).resolve().parent if source else Path.cwd()
        self.environment = data.get("environment")
        if self.environment not in (LIVE, SIMULATION):
            raise ConfigError("config.environment must be 'live' or 'simulation'")
        seren = _require(data, "seren", "config", dict)
        executable = _require(seren, "executable", "config.seren")
        if not os.path.isabs(executable):
            raise ConfigError("config.seren.executable must be an absolute path; the harness "
                              "does not search PATH for the Seren CLI")
        self.executable = executable
        self.executable_sha256 = seren.get("executable_sha256")
        if self.executable_sha256 is not None and not (
                isinstance(self.executable_sha256, str)
                and re.fullmatch(r"[0-9a-f]{64}", self.executable_sha256)):
            raise ConfigError("config.seren.executable_sha256 must be 64 lowercase hex digits")
        self.command_timeout = _positive(seren, "command_timeout_seconds", 60, "config.seren")
        self.poll_interval = _positive(seren, "poll_interval_seconds", 5, "config.seren")
        self.readiness_timeout = _positive(seren, "readiness_timeout_seconds", 300, "config.seren")
        self.shutdown_timeout = _positive(seren, "shutdown_timeout_seconds", 180, "config.seren")
        # Optional cap on Passwords apply attempts per provisioning run. Unset keeps the
        # default: an apply with an unknown outcome is issued again on resume (the CLI
        # answers already_applied). 1 means one attempt; later outcomes come from reads only.
        self.max_apply_attempts = seren.get("max_apply_attempts")
        if self.max_apply_attempts is not None and (
                type(self.max_apply_attempts) is not int or self.max_apply_attempts < 1):
            raise ConfigError("config.seren.max_apply_attempts must be a positive integer")
        # Optional explicit CLI session selection, passed to the CLI as XDG_CONFIG_HOME and
        # SEREN_PROFILE. Checked syntactically only: nothing is created, opened or run.
        self.config_home = seren.get("config_home")
        if self.config_home is not None and not (
                isinstance(self.config_home, str) and "\x00" not in self.config_home
                and os.path.isabs(self.config_home)
                and os.path.normpath(self.config_home) == self.config_home):
            raise ConfigError("config.seren.config_home must be an absolute, normalized path "
                              "(the CLI's XDG_CONFIG_HOME)")
        self.profile = seren.get("profile")
        if self.profile is not None and not (
                isinstance(self.profile, str) and _PROFILE.fullmatch(self.profile)):
            raise ConfigError("config.seren.profile must be one name of letters, digits, '.', "
                              "'_' or '-', starting with a letter or digit")
        employee = _require(data, "employee", "config", dict)
        self.employee_key = _require(employee, "key", "config.employee")
        self.deployment_id = _require(employee, "deployment_id", "config.employee")
        self.employee_names = [n for n in employee.get("names", []) if isinstance(n, str)]
        discord = data.get("discord", {})
        self.sender = discord.get("sender", "unavailable")
        self.observer = discord.get("observer", "unavailable")
        if self.sender not in SENDERS or self.observer not in OBSERVERS:
            raise ConfigError("config.discord.sender must be one of " + ", ".join(SENDERS)
                              + "; observer one of " + ", ".join(OBSERVERS))
        if self.environment == LIVE and SIMULATION in (self.sender, self.observer):
            raise ConfigError("a live run cannot use a simulated Discord sender or observer")
        self.discord = discord
        self.reply_timeout = _positive(discord, "reply_timeout_seconds", 120, "config.discord")
        # Optional: without it, reply content is retained but not judged.
        self.expected_reply = discord.get("expected_reply")
        if self.expected_reply is not None and not (
                isinstance(self.expected_reply, dict)
                and isinstance(self.expected_reply.get("template"), str)
                and self.expected_reply["template"].count("{marker}") == 1
                and isinstance(self.expected_reply.get("source"), str)
                and self.expected_reply["source"].strip()):
            raise ConfigError("config.discord.expected_reply needs a template containing "
                              "{marker} exactly once and a non-empty source")
        if self.observer != "unavailable":
            for key in ("channel_id", "employee_user_id"):
                _require(discord, key, "config.discord")
        if self.observer == "simulation" or self.sender == "simulation":
            self.simulation_channel = str(base / _require(
                discord, "simulation_channel_file", "config.discord"))
        self.github = data.get("github", {})
        self.include_identifiers = bool(data.get("disclosure", {}).get("include_identifiers"))
        self.state_dir = str(base / data.get("state_dir", ".seren-harness-state"))

    @property
    def digest(self):
        canonical = json.dumps(self.data, sort_keys=True).encode()
        return hashlib.sha256(canonical).hexdigest()[:16]

    def cli_session(self, environ=None):
        """The CLI session location the pinned CLI would use, without touching it.

        Explicit config values win. Otherwise it is what v0.10.0 resolves from the inherited
        environment: etcetera's Xdg strategy (also on macOS) takes XDG_CONFIG_HOME when it is
        absolute, else HOME/.config; the profile is SEREN_PROFILE trimmed, else `default`.
        """
        env = os.environ if environ is None else environ
        if self.config_home is not None:
            root, root_source = self.config_home, "explicit"
        elif os.path.isabs(env.get("XDG_CONFIG_HOME") or ""):
            root, root_source = env["XDG_CONFIG_HOME"], "inherited"
        elif env.get("HOME"):
            root, root_source = os.path.join(env["HOME"], ".config"), "inherited_default"
        else:
            root, root_source = None, "unresolved"
        if self.profile is not None:
            profile, profile_source = self.profile, "explicit"
        elif (env.get("SEREN_PROFILE") or "").strip():
            profile, profile_source = env["SEREN_PROFILE"].strip(), "inherited"
        else:
            profile, profile_source = "default", "inherited_default"
        return {"config_home": root, "config_home_source": root_source,
                "profile": profile, "profile_source": profile_source}

    def cli_env(self):
        """Environment for the CLI child process. Without an explicit selection: None, plain
        inheritance (documented fallback). With one: a copy of this process's environment
        with XDG_CONFIG_HOME / SEREN_PROFILE set to the configured values, overriding
        inherited ones, and SEREN_API_KEY removed so an inherited key cannot select another
        identity. The parent environment is never changed."""
        if self.config_home is None and self.profile is None:
            return None
        env = dict(os.environ)
        env.pop("SEREN_API_KEY", None)
        if self.config_home is not None:
            env["XDG_CONFIG_HOME"] = self.config_home
        if self.profile is not None:
            env["SEREN_PROFILE"] = self.profile
        return env

    def secret_env_names(self):
        names = []

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if _SECRET_REFERENCE.search(key) and isinstance(item, str):
                        names.append(item)
                    walk(item)
        walk(self.data)
        return names

    def secret_values(self):
        """Values of referenced secrets, loaded only to redact them from output."""
        return [os.environ[name] for name in self.secret_env_names() if os.environ.get(name)]


def load(path):
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError) as error:
        raise ConfigError("cannot read configuration " + str(path) + ": " + str(error))
    return Config(data, source=path)
