# ACM Agent Repository Guide

- Keep the runtime dependency-free: production code uses Python 3.13 standard library only.
- Treat `.acm/state.db` as the sole local state source; never infer AC from filenames or chat text.
- Platform AC is authoritative. `skipped` means mastered without implementation and is not AC.
- Preserve existing dated solution files and never expose arbitrary filesystem read/write APIs.
- Keep the HTTP server loopback-only and retain token, Host, Origin and request-size checks.
- Plan edits must remain transactional and revision-protected.
- Built-in plan sources are immutable; edits create managed overrides under `.acm/plans/`.
- Run `python -m unittest discover -s tests -v` after behavior changes.
- Run `python -m compileall -q tools tests` and `python -m tools.acm_agent plan check --json` before release.
- Do not commit `.acm/`, account identifiers, tokens, platform snapshots, solution history or generated reports.
