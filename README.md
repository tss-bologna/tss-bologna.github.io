# Editing the website

Pull the latest changes before editing: GitHub Actions also commits DBLP
cache updates. Commit and push to `main`; GitHub builds and publishes
automatically. Check **Actions → Build website** for errors or warnings.
No local installation is required.

## Files to edit

| Content | File |
|---|---|
| People | `data/people.yaml` |
| Talks | `data/talks.yaml` |
| News | `data/news.yaml` |
| Publication additions/exclusions | `data/publication_overrides.yaml` |
| Site settings and category labels | `data/site.yaml` |
| Home, seminar information, internal and legal text | `content/*.md` |
| Images and downloadable files | `static/` |
| Layout | `templates/` |
| Appearance | `static/css/style.css` |

Edit source files, not `_site/` or the generated DBLP cache.

## YAML and text

Use spaces, not tabs. Copy the commented templates in the data files.
Use quoted dates (`"2026-09-15"`) and unquoted `null` for absent values.
Remove a standalone `[]` when adding the first entry to an empty list.

Prose supports Markdown: `[label](URL)`, `*italics*`, `$inline math$`,
and `$$display math$$`. Follow each field's comments.

## People

Keep IDs stable. Sort by last name. Record departures rather than deleting
people; add periods for returns or status changes. Dates also determine
publication eligibility.

Photos accept a path relative to `static/`, an HTTPS image URL, or `null`.
Unavailable remote photos fall back to the SVG; build checks emit warnings.
`show_photos: false` in `site.yaml` hides all portraits.

## Talks

Keep newest talks first. Supply the room explicitly.
Times use Europe/Rome.

Keep `id` and `uid` unchanged when editing or rescheduling.
Update `last_modified` and increment `sequence` for calendar updates.
Use `cancelled: true` for cancellations.

## News

`date` controls publication; `expires` is the last day shown on Home.
Expired news remains in the archive. `expires: null` means no expiry.

## Publications

Selection uses eligible participation periods, by inclusive calendar year.
Use DBLP record keys in `publication_overrides.yaml` for corrections.
Informal publications, including CoRR preprints, are excluded even from
manual additions.

Pushes use the committed cache. DBLP refreshes daily.
After adding an uncached author or publication, run:
**Actions → Build website → Run workflow → Refresh DBLP cache**.

## Build controls

Repository variables are under:
**Settings → Secrets and variables → Actions → Variables**.

- `PAUSE_DBLP=true`: suspend fetching; builds continue using the cache.
  Delete it or set `false` to resume.
- `PUBLISH_SITE=true` and `demo: false` in `site.yaml`: enable deployment.
- `PUBLISH_SITE=false`: build previews without updating the live site.

Each successful build provides a `website-preview` artifact in Actions.
Disabling deployment leaves the existing live site online.
