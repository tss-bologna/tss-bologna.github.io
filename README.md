# Theory of Software Systems @ Bologna

A small static research website built with Python, Jinja2, YAML, and
Markdown. KaTeX renders mathematics during the build.

The published website contains HTML, CSS, fonts, images, and a calendar
feed. It requires no browser-side JavaScript.

## First build: no local dependencies required

Save the supplied files in their documented repository paths.

The repository's default branch should be `main`. If it has another name,
change the `push.branches` entry in `.github/workflows/site.yml`.

Keep `demo: true` in `data/site.yaml`. Do not create the `PUBLISH_SITE`
repository variable yet.

Commit and push the files. In GitHub:

1. Open the repository's **Actions** tab.
2. Select **Build website**.
3. Open the run triggered by your push.
4. If a run was not triggered, select **Run workflow** on `main`.
5. Inspect any failed step before continuing.
6. After a successful build, download **website-preview** from the run's
   artifacts.

GitHub installs the dependencies, runs tests, fetches DBLP, builds the pages,
and checks the output. Nothing is deployed at this stage.

The workflow also commits the generated `package-lock.json` and updated
`cache/dblp.json` when their contents change. Pull these changes before your
next local editing session.

An artifact named **generated-source-data** contains those two files as
well, in case you need to recover them after a failed Git push.

If Actions is disabled or organisation policy prevents the workflow from
writing repository contents, adjust the repository/organisation Actions
settings. This workflow needs permission to push its cache and lockfile
updates. It does not force-push or bypass branch protection.

## Reviewing the preview

Extract the `website-preview` archive.

The files constitute a complete static website. Navigation uses site-root
URLs, so opening individual HTML files through `file://` is not a full
navigation preview.

To browse the extracted site with correct navigation, use any local static
HTTP server. If Python is already installed, run this from the extracted
directory:

```bash
python3 -m http.server 8000
```

Then open http://localhost:8000/.

This needs no project dependencies and does not run the build. It only serves
the HTML/CSS that GitHub has already generated and checked.

Stop the server with Ctrl+C.

## Files to edit

| File | Purpose |
|---|---|
| `data/site.yaml` | Website settings, statuses, logo paths |
| `data/people.yaml` | People and participation periods |
| `data/talks.yaml` | Seminar events |
| `data/news.yaml` | News and expiry dates |
| `data/publication_overrides.yaml` | DBLP inclusions and exclusions |
| `content/home.md` | Home introduction |
| `content/seminar.md` | General seminar information and organisers |
| `content/internal.md` | Non-sensitive internal information |
| `content/legal.md` | Licensing and privacy notice |
| `static/css/style.css` | Appearance |
| `templates/` | HTML structure |

YAML fields are documented immediately before their first use.

Use spaces rather than tabs, quote dates and date-times, and preserve stable
IDs. Duplicate keys and unknown schema fields fail validation.

Optional fields can be omitted where their documentation provides a default.

## Before publication

- Replace illustrative people, news, and talks.
- Correct the illustrative arrival date in the Sangiorgi record.
- Add any remaining actual people and their DBLP identifiers.
- Replace the example organiser email address.
- Review publication selection, particularly arrival/departure years.
- Complete the licensing and privacy page.
- Supply logos and any portraits, with appropriate permission.
- Confirm the intended colour in `static/css/style.css`.
- Set `demo: false` in `data/site.yaml`.
- Review a successful build before enabling deployment.

The supplied Leblanc event is migrated from the provided PHP record.
The remaining sample talks are illustrative. The complete historical seminar
archive has not been migrated.

## Enable GitHub Pages

Only do this after reviewing the preview.

1. In the repository, open **Settings → Pages**.
2. Under the publishing source, select **GitHub Actions**.
3. Open **Settings → Secrets and variables → Actions → Variables**.
4. Add a repository variable:
   - Name: `PUBLISH_SITE`
   - Value: `true`
5. Run **Build website** again from the Actions tab.

Both conditions must hold before the deployment job runs:

- `PUBLISH_SITE` is exactly `true`;
- `demo` in `data/site.yaml` is `false`.

The expected address is https://tss-bologna.github.io/.

Once enabled, successful pushes, manual runs, and scheduled runs can update
the live site. Tests and generated-site checks run before deployment.

To pause future deployments, remove `PUBLISH_SITE` or change its value.
This does not remove the already published site.

## Routine editing

1. Pull the latest repository changes, including automatic cache commits.
2. Edit YAML, Markdown, templates, or CSS.
3. Commit and push.
4. Inspect the Actions result.

To refresh without editing files, use **Actions → Build website →
Run workflow**.

The workflow also runs daily at 05:23 UTC. Scheduled execution may be delayed;
the site does not update at an exact guaranteed instant.

GitHub may disable scheduled workflows in inactive public repositories.
Check the Actions tab if automatic refreshes stop.

## News

Home displays the three most recent active announcements by default.

An announcement is active from its publication date through its expiry date,
inclusive. An omitted or null expiry means no expiry.

Future announcements are not displayed. Expired announcements remain in
the archive.

If there is no active news, Home hides the entire news section and the
archive link. `/news/` remains accessible directly.

News Archive never appears in the main navigation.

## People and periods

Current people are grouped by their active status and sorted by surname,
then given name.

Arrival and departure dates are inclusive. Participation periods for one
person must not overlap. For a status transition, finish the old period
the day before the new one starts.

People with past periods but no active period appear in the compact former
section. Future-only records are not yet displayed.

The affiliation text is Markdown and controls display only. Publication
eligibility is controlled by participation periods and the status settings.

## Seminar

Upcoming talks are sorted earliest first; past talks are sorted latest first.

A talk becomes past at its starting time. This is evaluated at build time,
so a change becomes visible on the next successful build.

Warnings are displayed only for upcoming talks.

A missing speaker displays `TBA` for the speaker/title line. With a known
speaker but no title, the title is `TBA`. Abstracts are optional and collapsed
by default.

Each talk has its own location. An omitted location displays nothing.
General seminar information does not supply event locations.

Duration defaults to 60 minutes unless changed in the site settings or
overridden in a talk.

Cancelled talks remain listed and remain in the calendar with status
`CANCELLED`, but cannot become Home's next seminar.

## Calendar updates

The feed is `/seminar/calendar.ics`.

Preserve each event's `uid`, including after rescheduling. When modifying a
published event:

- update `last_modified`;
- increment `sequence`;
- keep the same `uid`.

To cancel an event, set `cancelled: true` and update its modification metadata.
Do not simply delete a published event if subscribers need to receive its
cancellation.

Calendar times are exported in UTC; the website displays Europe/Rome time.
Calendar applications normally display events in the subscriber's timezone.

Subscribe by URL for updates. Importing a downloaded file may create only a
snapshot. Subscription refresh frequency depends on the calendar application.

The page-anchor `id` and calendar `uid` are separate stable identifiers.

## Publications and DBLP

The fetcher retrieves complete bibliographies for people with a DBLP ID.
The build selects records associated with eligible participation periods.

Because DBLP generally supplies years rather than precise publication dates,
eligibility uses inclusive calendar years, capped at the current year.

This is an approximation at arrival/departure boundaries. Correct those
cases with `data/publication_overrides.yaml`.

- Inclusions bypass automatic affiliation filtering.
- Exclusions take precedence over inclusions.
- Keys must resolve to actual DBLP records.
- Duplicate publications are collapsed by DBLP record key.
- Same-year records are sorted by title.
- Twenty entries are initially visible; older entries are inside a native
  HTML disclosure.

A conference version and a journal version with different DBLP keys remain
separate records.

The cache is committed to support builds when DBLP is unavailable. The
fetcher writes nothing unless all requests succeed.

A failed refresh produces a workflow warning. The build can continue with
the previous cache only if it contains the authors and override records
required by the current source data.

An empty initial cache cannot support a bibliography build: at least one
successful fetch is required.

Snapshot timestamps change when the stored author bibliography changes,
not on every identical check. The displayed date is the oldest stored
author snapshot among the listed DBLP authors.

## Mathematics

Use `$...$` for inline mathematics.

Use `$$` on separate lines for display mathematics:

```yaml
abstract: |-
  An inline judgement: $\Gamma \vdash t : A$.

  $$
  \frac{\Gamma, x:A \vdash t:B}
       {\Gamma \vdash \lambda x.t : A \to B}
  $$
```

KaTeX renders HTML and MathML during the build. Invalid or unsupported
expressions fail the build with a diagnostic.

The build copies KaTeX CSS, fonts, and licence notices into the website.
No external font service or browser-side mathematics script is used.

## Internal and legal pages

`/internal/` is unlisted, omitted from the sitemap, and marked `noindex`.
It is public and must contain no sensitive information.

`/legal/` is linked from the footer only.

Example mode marks all pages `noindex` and produces an empty sitemap.
This discourages indexing; it is not access control.

## Optional local development

Local development is not required. GitHub Actions can perform all builds.

If desired, install Python 3.11+ and Node.js 22+, then:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm ci
python scripts/fetch_dblp.py
python -m unittest discover -s tests -v
python scripts/build.py
python scripts/check_site.py
python -m http.server 8000 --directory _site
```

Use `npm install` instead of `npm ci` only when no `package-lock.json` exists.
On Windows, virtual-environment activation differs from the shell command above.

For a reproducible build time:

```bash
python scripts/build.py --now 2026-09-14T12:00:00+02:00
```

## Validation scope

Behavioural tests cover key date boundaries, cancellation, publication
overrides, YAML duplicates, Markdown escaping, and KaTeX rendering.

The generated-site checker checks page structure, navigation, local links
and anchors, HTML asset references, calendar structure, and sitemap exclusions.

These are automated checks, not a complete accessibility or visual audit.
External links are not checked over the network.

## Dependencies

`package-lock.json` locks Node dependencies and is committed automatically
after the first successful workflow build.

Python dependencies currently use bounded version ranges in
`requirements.txt`; they are not fully locked. Review dependency changes
when maintaining the site.

## Generated files

Do not commit `_site/` or `node_modules/`.

Do commit:

- source data and content;
- templates and scripts;
- `cache/dblp.json`;
- `package-lock.json`.

Only the generated `_site/` directory is deployed.

## Licensing

See `LICENSE` for the code licence and `CONTENT-LICENSE.md` for its scope,
the editorial-content licence, and third-party exclusions.
