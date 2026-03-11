# Canvas Fetcher Documentation Notes
_Last updated: 2026-03-11_

## Source Sections Captured
- Get Started
- Quickstart Guide
- Canvas LMS OAuth2 Overview
- OAuth2 Endpoints
- Developer Keys

## 1) Get Started: What Matters
- Instructure Developer Docs cover APIs for Canvas LMS, DAP, Quizzes, and other products.
- For this project (course data backup), Canvas LMS API is the relevant product area.
- LLM-friendly full docs endpoint: `https://developerdocs.instructure.com/llms-full.txt`.

## 2) Quickstart (API Usage Pattern)
1. Obtain an access key/token (method depends on service).
2. Make API requests (Postman/curl/Test it).
3. Parse JSON responses:
   - Success: usually HTTP `200`/`201`.
   - Errors: HTTP `4xx` with `errors[].message`.
4. Handle pagination:
   - Canvas may split large lists into pages.
   - Use `Link` response header and follow `rel="next"` URLs until exhausted.

## 3) OAuth2 Core Concepts (Canvas LMS)
- Canvas uses OAuth2 (RFC 6749) for API auth.
- Developer keys issued after Oct 2015 produce access tokens with about 1 hour expiry.
- Applications should use refresh tokens to obtain new access tokens.
- Apps should store tokens securely and reuse them (do not request new tokens every time).

## 4) Token Storage and Security Requirements
Treat tokens like passwords:
- Do not embed tokens in web pages.
- Do not pass tokens/session IDs in URLs when avoidable.
- Secure token storage (DB/keychain/etc).
- Protect against XSS/CSRF/replay/session attacks.
- For native apps, use OS credential/keychain storage.

401 behavior:
- If token is deleted/expired, API returns `401 Unauthorized`.
- Differentiate auth failure by checking `WWW-Authenticate` header.

## 5) Manual Token Generation (Testing Only)
- You can generate a token at the Canvas profile page under Approved Integrations.
- This is acceptable for personal/testing workflows before full OAuth implementation.
- Asking multiple end users to manually generate/paste tokens violates Canvas API policy.
- Token value is shown once; if lost, generate a new one.

## 6) OAuth2 Authorization Code Flow (Canvas API)
### Step 1: Redirect user to Canvas auth
`GET https://<canvas-domain>/login/oauth2/auth?...`

Typical params:
- `client_id` (required)
- `response_type=code` (required)
- `redirect_uri` (required)
- `state` (recommended)
- `scope` (optional but required if key is scoped)
- optional: `purpose`, `force_login`, `unique_id`, `prompt`

### Step 2: Handle redirect response
Success:
`<redirect_uri>?code=XXX&state=YYY`

Error:
`<redirect_uri>?error=...&error_description=...&state=YYY`

Native app note:
- Canvas may redirect to a URL containing `code=<code>` in query.
- App should detect/extract this code in webview flow.

### Step 3: Exchange code for tokens
`POST /login/oauth2/token` with:
- `grant_type=authorization_code`
- `client_id`
- `client_secret`
- `redirect_uri` (if used in step 1)
- `code`
- optional `replace_tokens=1` to replace existing tokens for that key

Important:
- Authorization code is one-time use; reused code fails.

## 7) Using Access Tokens
Recommended:
- Send via Authorization header:
  `Authorization: Bearer <ACCESS_TOKEN>`

Supported but discouraged:
- Query string or POST parameter `access_token=...` (higher leakage risk).

## 8) Refresh Token Flow
Access tokens expire in about 1 hour.

Refresh request:
`POST /login/oauth2/token` with:
- `grant_type=refresh_token`
- `client_id`
- `client_secret`
- `refresh_token`

Notes:
- Canvas returns a new access token.
- Refresh token usually stays the same (not rotated in response).

## 9) OAuth2 Endpoints (Reference)
- `GET /login/oauth2/auth`
- `POST /login/oauth2/token`
- `DELETE /login/oauth2/token` (revoke own token; optional `expire_sessions=1`)
- `GET /login/session_token` (create temporary web session URL from API token)

## 10) Grant Types Supported by `POST /login/oauth2/token`
- `authorization_code` (Canvas API user flow)
- `refresh_token` (renew access token)
- `client_credentials` (LTI Advantage services, JWT client assertion required)

## 11) Special Case: `scope=/auth/userinfo`
- If auth request uses `scope=/auth/userinfo`, token exchange returns user identity info.
- Response contains `access_token: null` (identity-only flow).

## 12) Developer Keys (Critical for OAuth Apps)
- Developer keys are Canvas OAuth client ID/secret pairs.
- Keys can be root-account scoped or global (if issued by Instructure).
- Key must be enabled for the account or OAuth/API calls fail (`unauthorized_client`/`401`).

### Scoped vs unscoped keys
- Unscoped key: token can access all resources available to user.
- Scoped key: token limited to enabled endpoint scopes.

Scope format:
`url:<HTTP_VERB>|<API_PATH>`

Example:
`url:GET|/api/v1/courses/:course_id/rubrics`

If a scoped key requires scopes and none are requested:
- OAuth call returns `invalid_scope`.

## 13) Scope and Token Behavior Changes
- If scopes are removed from a key, existing tokens can be invalidated.
- If an unscoped key becomes scoped, old tokens stop working; new scoped request needed.
- If a scoped key becomes unscoped, existing tokens continue and effectively broaden.
- Scoped tokens with include params:
  - include/includes work only if Allow Include Parameters is enabled.

## 14) Practical Guidance for `canvas-fetcher`
For a local single-user backup tool:
1. Start with a manually generated token (fastest for personal use/testing).
2. Use header auth: `Authorization: Bearer <token>`.
3. Always implement pagination via `Link` headers.
4. Expect and handle `401` (expired/revoked token).
5. For production/multi-user usage, move to full OAuth authorization code flow plus refresh tokens.
6. If using scoped developer keys, request only required scopes and handle `invalid_scope`.

## 15) Notes on LTI `client_credentials`
- Intended for LTI Advantage service access, not standard user Canvas API scraping.
- Requires signed JWT (`client_assertion`) and IMS scopes.
- Usually not needed for assignments/modules fetcher use case.

## 16) Online Docs Verified for Student Course Fetching
The sections below were re-verified from public Canvas docs online (Mar 11, 2026), focused on student-access course backups.

Primary references:
- https://canvas.instructure.com/doc/api/modules.html
- https://canvas.instructure.com/doc/api/assignments.html
- https://canvas.instructure.com/doc/api/pages.html
- https://canvas.instructure.com/doc/api/files.html
- https://canvas.instructure.com/doc/api/courses.html
- https://canvas.instructure.com/doc/api/tabs.html
- https://canvas.instructure.com/doc/api/file.pagination.html
- https://canvas.instructure.com/doc/api/file.permissions.html

Docs move notice:
- Legacy pages state docs have moved to https://developerdocs.instructure.com/services/canvas and old pages will redirect after July 1, 2026.

## 17) Does This Work for Students?
Yes, with role and visibility limits.

What students can fetch:
- Course data that is visible to that student account and enrollment.
- Published/accessible modules, assignments, pages, and files in enrolled courses.

What students usually cannot fetch:
- Instructor/admin-only content.
- Unpublished or locked content when they do not have permission.
- Content hidden by assignment overrides, module prerequisites, or tab visibility rules.

Evidence from resource objects/endpoints:
- Modules include per-user state and may include `published` only if caller can view unpublished modules.
- Assignments can return `published`, `only_visible_to_overrides`, `locked_for_user`, `lock_explanation`.
- Pages can return `published`, `locked_for_user`, `lock_explanation`.
- Files can return `hidden_for_user`, `locked_for_user`, and lock metadata.

## 18) Core Read Endpoints for a Course Backup Tool
Recommended read-only endpoints and scopes:

1. Discover courses for current user
- `GET /api/v1/courses`
- Scope: `url:GET|/api/v1/courses`
- Useful filters: `enrollment_type=student`, `state[]=available`, `per_page=100`
- Docs note that default returned state is typically `available` for students/observers.

2. Modules and module items
- `GET /api/v1/courses/:course_id/modules`
- Scope: `url:GET|/api/v1/courses/:course_id/modules`
- Optional include: `include[]=items`, `include[]=content_details`
- If items are omitted for large modules, call:
  - `GET /api/v1/courses/:course_id/modules/:module_id/items`
  - Scope: `url:GET|/api/v1/courses/:course_id/modules/:module_id/items`

3. Assignments
- `GET /api/v1/courses/:course_id/assignments`
- Scope: `url:GET|/api/v1/courses/:course_id/assignments`
- Optional includes: `submission`, `all_dates`, `overrides` (as needed)

4. Pages
- `GET /api/v1/courses/:course_id/pages`
- Scope: `url:GET|/api/v1/courses/:course_id/pages`
- Use `include[]=body` to include page body in listing, or call page detail endpoint:
  - `GET /api/v1/courses/:course_id/pages/:url_or_id`
  - Scope: `url:GET|/api/v1/courses/:course_id/pages/:url_or_id`

5. Files and folders
- `GET /api/v1/courses/:course_id/folders`
- Scope: `url:GET|/api/v1/courses/:course_id/folders`
- `GET /api/v1/courses/:course_id/files`
- Scope: `url:GET|/api/v1/courses/:course_id/files`
- Optional file detail: `GET /api/v1/files/:id` (scope `url:GET|/api/v1/files/:id`)

6. Navigation/tab visibility (useful for debugging missing content)
- `GET /api/v1/courses/:course_id/tabs`
- Scope: `url:GET|/api/v1/courses/:course_id/tabs`
- Tab objects include `hidden` and `visibility`.

## 19) Pagination Rules You Must Implement
From Canvas pagination docs:
- Most list endpoints default to 10 items/page.
- You can request `per_page`, but max is unspecified.
- Always follow the HTTP `Link` header (treat links as opaque).
- Parse `Link` header case-insensitively.
- If auth is done via query `access_token`, pagination links may omit token; header auth avoids this issue.

## 20) Auth Guidance for Student-Only Personal Backup
For personal local backup, practical approach:
- Use manual token generation for your own account during testing.
- Send token in `Authorization: Bearer <ACCESS_TOKEN>` header.
- For shared/multi-user apps, use full OAuth2 Authorization Code flow.

Policy note from Canvas docs:
- Manual token copy/paste by many end users is not acceptable for production multi-user apps.

## 21) Permission and Role Notes Relevant to Fetchers
From permissions docs:
- Course-level content/file editing and upload actions are tied to explicit permissions (e.g., manage files/content).
- Some actions are role-limited (typically teacher/TA/designer/admin), while read access depends on enrollment/visibility.
- Lack of permission can return `401/403` or result in hidden/omitted data.

Important practical implication:
- Student fetchers should be read-only and resilient to partial visibility.

## 22) Content You Might Not Fully Capture via API
Even with correct token and scopes:
- External tool items (`ExternalTool`) may point to LTI apps where content is not directly downloadable by Canvas REST alone.
- External URLs in modules are links, not hosted Canvas content.
- Some downstream resources may require browser session/LTI launch flow beyond simple REST fetch.

## 23) Recommended Minimal Sync Order (Per Course)
1. Fetch course metadata (`/courses/:id` optional).
2. Fetch modules (`include[]=items&include[]=content_details`).
3. Fetch assignments.
4. Fetch pages list and bodies.
5. Fetch folders/files metadata.
6. Download file binaries for files the student can access.
7. Save raw JSON plus normalized local index.

## 24) Suggested Local Storage Layout
Example:
- `/home/elvar/Documents/School/Canvas/<course_id>_<course_name>/raw/course.json`
- `/home/elvar/Documents/School/Canvas/<course_id>_<course_name>/raw/modules.json`
- `/home/elvar/Documents/School/Canvas/<course_id>_<course_name>/raw/module_items_<module_id>.json` (if needed)
- `/home/elvar/Documents/School/Canvas/<course_id>_<course_name>/raw/assignments.json`
- `/home/elvar/Documents/School/Canvas/<course_id>_<course_name>/raw/pages.json`
- `/home/elvar/Documents/School/Canvas/<course_id>_<course_name>/raw/files.json`
- `/home/elvar/Documents/School/Canvas/<course_id>_<course_name>/files/<file_id>_<filename>`

Keep both:
- Original API payloads (for traceability)
- A simplified merged index for easy local searching/reporting

## 25) Implemented Incremental Sync Notes (`canvas_fetcher.py`)
Current script support:
- `--incremental`: enables incremental behavior.
- Reuses cached nested module items when module payload is unchanged and prior fallback items exist.
- Reuses cached page details when page `updated_at` is unchanged.
- Writes `raw/changes.json` with added/updated/removed summaries per resource.
- Writes `sync_state.json` with run timestamp and dataset hashes.

Important limitation:
- Canvas list endpoints used here generally do not expose a universal `updated_since` filter, so top-level lists are still fetched each run.
- Incremental mode primarily reduces nested follow-up calls and improves change visibility.

## 26) My Canvas Domain and Course IDs (Editable)
Use this section and `courses.json` as your local reference for running `canvas_fetcher.py`.

Canvas domain:
- `https://reykjavik.instructure.com`

Saved course IDs:
- `10053` (from `https://reykjavik.instructure.com/courses/10053`)

Add more here as you collect them:
- `<course_id>`
- `<course_id>`

Optional command template:

```bash
python canvas_fetcher.py --base-url "https://reykjavik.instructure.com" --course-id "10053" --incremental
```

## 27) Saved Config File for One-Command Multi-Course Sync
Saved config path:
- `/home/elvar/Documents/Projects/canvas-fetcher/courses.json`

Current file content:

```json
{
  "base_url": "https://reykjavik.instructure.com",
  "course_ids": [
    "10053"
  ]
}
```

Run all saved courses:

```bash
python canvas_fetcher.py --use-saved --incremental
```

Notes:
- Add additional course IDs inside `course_ids`.
- You can still override/add course IDs at runtime by repeating `--course-id`.

## 28) Local Save Path Preference
Configured default output directory for this project:
- `/home/elvar/Documents/School/Canvas`

So running the script without `--out-dir` will save course backups there.

Folder naming rule:
- Each course folder now includes both ID and course name, for example `10053_Intro-to-Biology`.
- If an older ID-only folder exists (for example `10053`), the script will try to rename it to the new format automatically.

## 29) Project-Only Token Setup
Project token file path:
- `/home/elvar/Documents/Projects/canvas-fetcher/.env`

Expected format:

```bash
CANVAS_TOKEN="<your_canvas_access_token>"
```

Notes:
- This keeps the token local to this project workflow.
- `canvas_fetcher.py` will auto-read `.env` from the current directory or script directory if `CANVAS_TOKEN` is not already set in the shell.

## 30) Structured Assignment and Module Exports
Per-course folder now includes human-browsable exports in addition to raw API JSON:

- `assignments/`
  - one directory per assignment (ordered)
  - `assignment.json`
  - `description.html`
  - `linked_files.json`
  - `files/` containing files referenced in assignment description/attachments

- `modules/`
  - one directory per module, then one directory per module item (ordered hierarchy)
  - module root includes `module.json` and `index.json`
  - each item folder includes `item.json`, `linked_files.json`, and optional `content.html`
  - `files/` inside each item folder contains linked/required files for that item

Implementation note:
- Files are cached in `/.linked_file_cache/` and copied into each assignment/module location so browsing is easy while avoiding repeated downloads.
