## etabs-mcp v0.3.0

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
