# Security

## Reporting

Open a private security advisory via GitHub, or email the maintainer. Please do not open a
public issue for a vulnerability.

## Threat model, stated honestly

EvalForge v0.2 executes scorers **in-process**. `egress.blocked()` denies network access at the
stdlib socket entry points during tier-0 scoring, which stops accident and casual misuse. It is
**not** a sandbox against hostile code: determined code can re-import a fresh socket module
from disk.

**Therefore: only run scorers you trust.** Third-party scorer sandboxing (subprocess + no
network namespace + seccomp) is on the v0.4 roadmap and is a prerequisite for running untrusted
plugins.

## Known non-issues

- Budget overrun is a *feature* of the accounting, not a vulnerability: `settle` records the
  excess as `overrun` rather than hiding it, and any overrun makes a run ineligible to pass.
