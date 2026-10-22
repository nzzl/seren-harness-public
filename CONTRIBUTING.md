# Contributing

Read AGENTS.md and DEVELOPMENT.md. Develop offline against the synthetic stand-ins, run the
required checks (`python3 .tiny-harness/run.py validate` in a Git working tree with the
development environment active) and keep credentials, operator configurations and runtime
reports out of commits. Changes must not weaken safety checks or enable live actions by
default.
