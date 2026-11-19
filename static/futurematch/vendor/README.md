# Vendored browser libraries

Served from the app itself so a page never depends on a CDN being reachable (and so the
browser journey in `tests/e2e/`, which blocks other hosts, sees working charts and chat).
The files are byte-identical to the published builds; the directory name carries the
version. To upgrade: download the new build, verify its hash against the publisher's,
put it in a new versioned directory, update the template references, delete the old one.

| Library | Version | Licence | Published at | sha384 |
|---|---|---|---|---|
| Chart.js | 4.4.0 | MIT | `cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js` | `e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g` |
| marked | 12.0.0 | MIT | `cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js` | `NNQgBjjuhtXzPmmy4gurS5X7P4uTt1DThyevz4Ua0IVK5+kazYQI1W27JHjbbxQz` |
| QRCode.js | 1.0.0 | MIT | `cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js` | `3zSEDfvllQohrq0PHL1fOXJuC/jSOO34H46t6UQfobFOmxE5BpjjaIJY5F2/bMnU` |

The mind map still loads React, three.js and Babel from CDNs, pinned to exact versions with
Subresource Integrity (`templates/fm/mind_map.html`, `assets/mind-map-support.js`).
