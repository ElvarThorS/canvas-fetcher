# canvas-fetcher

Simple Canvas LMS course backup script (read-only).

## What it fetches

- Course metadata
- Modules (with inline items when available)
- Module items fallback per module (when inline module items are omitted)
- Assignments
- Pages (with page body when available)
- Folders and files metadata
- Course tabs
- Structured course export:
  - `assignments/` with one folder per assignment (`assignment.json`, `description.html`, linked files)
  - `modules/` following module/item order with linked files copied into each item folder

Optional:
- Download file binaries accessible to the authenticated user

## Requirements

- Python 3.9+
- `requests` (`pip install requests`)

## Usage

Set your token in env:

```bash
export CANVAS_TOKEN="<your_canvas_access_token>"
```

Project-only option (auto-loaded, recommended):

Copy `Documents/Projects/canvas-fetcher/.env.example` to `.env` and set your token:

```bash
CANVAS_TOKEN="<your_canvas_access_token>"
CANVAS_FETCHER_OUT_DIR="/absolute/path/for/backups"
CANVAS_FETCHER_SAVED_CONFIG="/absolute/path/to/courses.json"
```

If `CANVAS_TOKEN` is not set in your shell, the script automatically checks `.env` in:
- your current working directory
- the script directory (`Documents/Projects/canvas-fetcher`)

The script also reads these optional path settings from shell env or `.env`:
- `CANVAS_FETCHER_OUT_DIR` (default fallback: `backup`)
- `CANVAS_FETCHER_SAVED_CONFIG` (default fallback: `courses.json`)

Run a course backup:

```bash
python canvas_fetcher.py \
  --base-url "https://<your-canvas-domain>" \
  --course-id "12345"
```

Run multiple courses by repeating `--course-id`:

```bash
python canvas_fetcher.py \
  --base-url "https://<your-canvas-domain>" \
  --course-id "12345" \
  --course-id "67890"
```

Run all saved courses from `courses.json` in one command:

```bash
python canvas_fetcher.py --use-saved --incremental
```

Use a different saved config file:

```bash
python canvas_fetcher.py --use-saved --saved-config "my_courses.json"
```

Download files too:

```bash
python canvas_fetcher.py \
  --base-url "https://<your-canvas-domain>" \
  --course-id "12345" \
  --download-files
```

Incremental sync (reuses unchanged nested data when possible and writes a change report):

```bash
python canvas_fetcher.py \
  --base-url "https://<your-canvas-domain>" \
  --course-id "12345" \
  --incremental
```

Limit how many IDs appear per change bucket in the report:

```bash
python canvas_fetcher.py \
  --base-url "https://<your-canvas-domain>" \
  --course-id "12345" \
  --incremental \
  --change-id-limit 100
```

Pass token directly (alternative):

```bash
python canvas_fetcher.py \
  --base-url "https://<your-canvas-domain>" \
  --course-id "12345" \
  --token "<your_canvas_access_token>"
```

## Output layout

Default output directory is `backup/` (unless overridden by `CANVAS_FETCHER_OUT_DIR` or `--out-dir`):

```text
backup/
  run_summary.json
  <course_id>_<course_name>/
    manifest.json
    assignments/
      index.json
      <assignment_folder>/
        assignment.json
        description.html
        linked_files.json
        files/
    modules/
      index.json
      <module_folder>/
        module.json
        index.json
        <item_folder>/
          item.json
          content.html
          linked_files.json
          files/
    raw/
      course.json
      modules.json
      module_items_by_module.json
      assignments.json
      pages.json
      page_details.json
      folders.json
      files.json
      tabs.json
      download_results.json
      changes.json
      structured_export_warnings.json
    files/
      <file_id>_<file_name>
    .linked_file_cache/
    sync_state.json
```

`courses.json` format (you can copy `courses.json.example`):

```json
{
  "base_url": "https://reykjavik.instructure.com",
  "course_ids": ["10053", "12345"]
}
```

If you do not want `courses.json` in Git, keep your real file local and set:

```bash
CANVAS_FETCHER_SAVED_CONFIG="/absolute/path/to/courses.json"
```

(`courses.json` is gitignored in this project.)

If `--saved-config` is a relative path, the script checks your current directory first, then the script directory.

Course folders are named with both ID and name (sanitized), e.g. `10053_Intro-to-Biology`.

## Notes

- The script follows Canvas pagination via the `Link` header.
- It retries on transient failures (`429`, `500`, `502`, `503`, `504`).
- A student token only returns content visible to that student account.
- In incremental mode, some endpoints still require full list fetches (Canvas does not provide `updated_since` filters for all resources), but the script reuses unchanged nested resources where possible and reports diffs in `raw/changes.json`.
