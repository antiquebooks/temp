# Guest feedback form

Anonymous feedback for 3 flats and 1 house. Guests scan a QR code, give 1-5 stars, optionally tap topics and tags, and send. Low ratings (3 or below) email you in English. Every response lands in a Google Sheet. For the house only, a 5-star response gets an invitation to review on Google.

Built to the v3 spec: no personal data, no accounts, no external fonts or trackers, no dependencies.

## The four QR codes

Replace `YOUR-SITE` with your Netlify address (or custom domain), then generate one QR code per link with any QR generator. Print each code 2-3 times for the same property.

| Property | Airbnb listing | Link for the QR code |
|---|---|---|
| Flat 1: Modern & Historic Apartment | airbnb.com/rooms/12894253 | `https://YOUR-SITE.netlify.app/?p=flat1` |
| Flat 2: Traditional Slovak Apartment | airbnb.com/rooms/13920795 | `https://YOUR-SITE.netlify.app/?p=flat2` |
| Flat 3: Castle Hill Quiet Apartment | airbnb.com/rooms/50237798 | `https://YOUR-SITE.netlify.app/?p=flat3` |
| House: Forestside Raceway Hideaway | airbnb.com/rooms/1235686920970090165 | `https://YOUR-SITE.netlify.app/?p=house` |

Add `&lang=de` (or `sk`, `es`, `fr`, `en`) to force a language. Normally the page follows the guest's phone language and falls back to English.

## Setup (about 30 minutes)

### 1. Google Sheet for responses

1. Create a Google Sheet. Rename the first tab to `Responses`.
2. Paste the header row from `setup/responses-header-row.csv` into row 1.
3. Optional: add a second tab `Improvements` with `setup/improvements-header-row.csv` (issue, action, date fixed).
4. In [Google Cloud Console](https://console.cloud.google.com), create a project and enable the **Google Sheets API**.
5. Create a **service account** (IAM & Admin > Service accounts), then add a **JSON key** to it.
6. Share the Sheet with the service account's email address as **Editor**.
7. From the JSON key you need `client_email` and `private_key`. The Sheet ID is the long part of the Sheet's URL.

### 2. Alert email (Resend)

1. Create a free account at [resend.com](https://resend.com) and make an API key.
2. Out of the box emails go out from `onboarding@resend.dev`, which Resend only delivers to the address you signed up with. That is enough if alerts go to you. To send to another address, verify a domain in Resend and set `ALERT_FROM_EMAIL`.

### 3. Translation of alerts (optional)

Enable the **Cloud Translation API** in the same Google Cloud project and create an **API key**. Restrict the key to that API. Without it, alerts still arrive but comments are not translated. The free monthly quota is far above this use.

### 4. Deploy to Netlify

Put this folder in a Git repository and import it in Netlify (it reads `netlify.toml`), or run `npx netlify-cli deploy --prod` from this folder. Then add the variables from `.env.example` under Site configuration > Environment variables and redeploy.

`GOOGLE_REVIEW_URL_HOUSE` can stay empty for now. Until it is set, the Google invitation simply does not appear.

### 5. Test before printing

- Open each of the four links on your phone. Send a 2-star response with a tag and a comment in Slovak: you should get an English alert email and a new row in the Sheet.
- Send a 5-star response for the house after setting the Google URL: the invitation should appear. For the flats it never does.
- Delete the test rows afterwards.

To try it on your computer first: `npm i -g netlify-cli`, then `DRY_RUN=1 netlify dev`. Rows and emails are printed in the terminal instead of being sent. `npm test` runs the automated checks.

## How it behaves

| Rule | Behavior |
|---|---|
| Required input | The star rating only |
| 3 stars or below | Prompt "What should we fix?" with topics shown prominently; alert email in English |
| 4-5 stars | Prompt "Anything we could still improve?" with topics in a lighter style |
| Google review | House and 5 stars only. The link is sent by the server only in that case |
| Stored data | Time, property, language, rating, tags, comments, English translation (low ratings), alert status |
| Not stored | Names, contact details, stay dates, IP addresses |
| Spam protection | Hidden honeypot field, minimum fill time (1.5 s), size and length limits, whitelist of properties and tags, light per-IP rate limit (8 per 10 minutes, best effort) |

## Reading the Sheet

- Tags are stored as `category:tag`, for example `condition:hotWater`. To count one tag for one property: `=COUNTIFS(B:B,"Flat 2",E:E,"*condition:hotWater*")`.
- The `Alert` column shows `sent`, `FAILED` or `-`. Check it if an expected email did not arrive.
- If the Sheet is unreachable, a low-rating alert email still goes out and the guest still sees "Thank you". High ratings are only confirmed once the Sheet accepted them, otherwise the guest is asked to try again.

## Changing things

- **Tags per property, property names, Google gating:** `public/config.json`. A new tag also needs a label in all five languages in `public/i18n.js`. `npm test` fails if a label is missing.
- **Wording and translations:** `public/i18n.js`. The five languages should be read once by a native speaker before printing.
- **Colors:** each property has its own accent at the top of `public/styles.css`.
- **Fonts:** the page uses system fonts, so nothing is loaded from third parties. If you want a specific typeface, self-host the font files from `public/` and update the CSP in `netlify.toml`.
- **Alert threshold:** `LOW_RATING_MAX` in `netlify/functions/submit.js`.

## Files

```
public/            the page (index.html, styles.css, app.js, i18n.js, config.json)
netlify/functions/ submit.js: validates, alerts, stores, decides on the Google link
netlify/lib/       Google Sheets, translation, alert email, validation
test/              automated checks (npm test)
setup/             header rows for the Sheet
netlify.toml       publish folder, /api/submit route, security headers
```

## Good to know

- **Google policy:** inviting only 5-star guests is "review gating", which Google's policies discourage. You chose this knowingly, so the risk is that Google may remove reviews.
- **Honest scope of "anonymous":** the form asks for no identity and stores no IP address. Netlify and your email provider keep their own standard server logs outside this project.
- **Privacy notice:** the page does not set cookies or load third-party resources. Whether you need to mention it in your guest information depends on local rules, so check this with whoever advises you on that.
