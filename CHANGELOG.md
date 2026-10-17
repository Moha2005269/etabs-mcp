# Changelog

## v0.2.0

Install the wheel attached below, then connect your agent (see the README):

```
pip install etabs_mcp-0.2.0-py3-none-any.whl
etabs-mcp --list-installs
```

Requires Windows, ETABS 22 or newer, and Python 3.10+.

### New: see and query the model
- `etabs_render_view`: offscreen renders from any angle, deformed shapes of any case, members coloured by design ratio, displacement, section or story. Works with ETABS hidden and never takes focus.
- `etabs_find_objects`: engineering queries (role, story, section, group, D/C range, length, bounding box), optionally selecting the matches.

### New: lateral system
- `etabs_define_seismic_load` (ASCE 7-16 ELF), `etabs_define_wind_load` (ASCE 7-16/7-22, other codes via table fields), `etabs_define_response_spectrum`, `etabs_set_mass_source`, `etabs_assign_diaphragm`, `etabs_set_modal_case`.

### New: workflow
- Background analysis and design (`background=true`) with `etabs_job_status` and `etabs_job_wait`. Long runs no longer hit client tool timeouts; other calls return a clear `busy` error while a job runs.
- `etabs_what_changed` detects hand edits made in the ETABS window.
- `etabs_checkpoint`, `etabs_list_checkpoints` and `etabs_restore_checkpoint` give undo points before risky edits.
- `etabs_export_model` returns the full model as `.e2k` text (paged, filterable by section).
- `etabs_design_report` writes a Markdown engineering summary.
- `etabs_assign_label` assigns groups, piers and spandrels.
- Lengths accept `"6 m"`, `"20 ft"`, `"250 mm"` or `12'6"`.

### Fixes
- Design errors (for example, sections that are not seismically compact under AISC 341) are reported and drawn as failing, never as ratio 0.
- Existing diaphragms such as `D1` are reused instead of failing.
- The `mcp` dependency is pinned below 2.0.

60 tools in total. Verified with 49 offline tests plus two live end-to-end suites against ETABS 23.3.2.

## v0.1.0

First release: 43 tools, generic access to the full ETABS API, live end-to-end tests.
