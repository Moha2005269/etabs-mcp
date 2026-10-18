# Changelog

## v0.3.1

**Now on PyPI and in the MCP Registry.**

```
pip install etabs-mcp
```

Or run it without installing: `uvx etabs-mcp`. For Claude Desktop, download the `.mcpb` matching your Python (`py311`, `py312` or `py313`) below and open it.

Requires Windows and ETABS 22 or newer.

### Changes since v0.3.0
- Published to PyPI with trusted publishing, and listed in the official MCP Registry as `io.github.moha2005269/etabs-mcp`.
- Linux container support for registries such as Glama: a `Dockerfile` and `glama.json`. The server starts and lists its tools on Linux, and connecting explains that ETABS needs Windows. CI builds the image and runs an MCP handshake inside it on every push.

71 tools. See the [v0.3.0 notes](https://github.com/Moha2005269/etabs-mcp/releases/tag/v0.3.0) for the full feature list.

## v0.3.0

**Claude Desktop:** download the `.mcpb` matching your Python (`py311`, `py312` or `py313`) and open it. It's a one-click install with all dependencies bundled.
**Any other MCP client:** `pip install` the wheel below, then see the README.

Requires Windows and ETABS 22 or newer.

### New: generate buildings
- `etabs_generate_building` builds a complete building in one call: grids (any bay widths), stories, steel moment or braced frames, concrete frames or shear walls with pier labels, slabs, fixed supports, a rigid diaphragm, SDL/LL loads and design combinations.

### New: concrete and shear-wall design
- `etabs_get_concrete_design` covers beams, columns and joints, with required reinforcement and PMM ratios.
- `etabs_run_shear_wall_design` runs ETABS's shear wall design. The ETABS API has no call for it, so the server invokes the menu command through Windows UI Automation and reads the results back through the API.
- `etabs_get_wall_design` returns pier and spandrel results; `etabs_get_slab_design` returns strip and punching-shear results.
- `etabs_design_codes` lists, reads and sets the steel and concrete design codes.

### New: code checks
- `etabs_code_checks` runs, in one call: story drift against the allowable (Cd-amplified), torsional, soft-story and weight irregularity, modal mass participation, and RS vs ELF base shear. Each check returns its clause, numbers and pass/fail.
- `etabs_scale_response_spectrum` scales a spectrum case to the ELF base shear, re-runs and verifies.

### New: production features
- Drive several ETABS instances from one server (`etabs_connect(instance=...)`, `etabs_instances`).
- An audit log of every model-changing call (`--audit-log`).
- Bearer-token auth for the HTTP transports (`--token`); the server refuses to listen beyond localhost without one.
- `etabs_menu` and `etabs_dialogs` reach ETABS menu commands that have no API.

### Distribution
- Claude Desktop extensions (`.mcpb`) are attached to every release.
- The release workflow can publish to PyPI and the MCP Registry with tokenless trusted publishing (see README › Publishing).

### Fixes
- Pier and spandrel labels are reused when they already exist; ETABS creates `P1`… automatically for walls.
- Drift results keep full precision.
- An empty joint-design result is explained, not reported as an error.
- ETABS is found even when the host starts the server with a minimal environment.

71 tools. Verified with 58 offline tests and three live end-to-end suites against ETABS 23.3.2.

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
