# Guest Feedback System: Conceptual Specification

**Status:** Final concept, ready for build planning
**Scope:** 3 flats + 1 house
**Target stack:** Netlify (static page + serverless function), Google Sheets, email alerts

---

## 1. Purpose and principles

- **Internal improvement first.** Collect comments that help fix problems. Ratings are only a signal, never published or ranked.
- **Anonymous by design.** No names, stay dates or contact details. The only identification happens on Google, where the guest logs in themselves.
- **Low effort.** Under 2 minutes, mobile-first, no login, no app.
- **QR access only**, placed locally in each property. No links sent by message.
- **Guest contact stays in the booking system.** The form never asks for contact details.

---

## 2. Access and QR codes

- **One QR code per property**, printed and placed at 2-3 spots inside it (e.g. entrance, kitchen, bedside).
- All copies of a property's code lead to the **same page**, e.g. `https://<yoursite>.netlify.app/?p=house`.
- Placement is not tracked, so there is no per-location data.
- **Four codes in total:** Flat 1, Flat 2, Flat 3, House.
- The property is identified from the link, so guests never choose it.

---

## 3. Language

- Supported: **EN, SK, ES, FR, DE**.
- The page opens in the guest's **phone/browser language** if it is one of these; otherwise English.
- A **small language selector** (EN / SK / ES / FR / DE) is always visible at the top.
- Category and tag labels are translated once. Free-text comments stay in the guest's own language.

---

## 4. Guest flow

1. Guest scans the QR code. The form opens in the detected language, with the property pre-identified.
2. **Overall rating: 1-5 stars.** This is the only required input.
3. **Optional category topics.** Guest taps only the ones where they have something to say. Each opens quick-select tags plus a comment box.
4. Optional general comment, then submit.
5. **Thank-you screen.** House only: a 5-star rating adds the Google review invitation with a direct link.

**Prompting depends on the rating:**

| Rating | Prompt | Topics display |
|---|---|---|
| 3 or below | "What should we fix?" | Prominent |
| 4-5 | "Anything we could still improve?" | Lighter style |

---

## 5. Categories and tags

Categories are **comment topics, not rating scales**. Nothing is mandatory.

| Category | What it covers | Example tags |
|---|---|---|
| Cleanliness | Arrival state, bathroom, kitchen, linens | Bathroom, Kitchen, Bedding, Floors, Smell |
| Comfort & Sleep | Beds, noise, temperature, space | Bed/mattress, Noise, Too hot, Too cold, Pillows |
| Equipment & Amenities | Kitchen items, Wi-Fi, appliances, supplies | Wi-Fi, TV, Cookware, Washing machine, Toiletries |
| Condition & Maintenance | Anything broken, worn or not working | Hot water, Heating/AC, Lighting, Leak, Broken item |
| Check-in & Communication | Instructions, key access, responsiveness | Instructions unclear, Key/access, Slow reply |
| Location & Surroundings | Neighborhood, parking, access | Parking, Street noise, Public transport |

- Tag lists are **defined per property**. The house can add items such as garden or BBQ.
- Tags make analysis easy (count how often "Wi-Fi" appears per property); comments supply the detail.

---

## 6. Google review logic (house only, strict gating)

| Property | Overall rating | Outcome |
|---|---|---|
| House | 5 stars | Thank-you screen with invitation and direct Google review link |
| House | 1-4 stars | Thank-you only, noting that feedback will be reviewed |
| Flats (all 3) | Any | Thank-you only, no Google link |

- The Google link is **not yet available**. It will be added later as a single configuration value for the house, so the invitation stays hidden or disabled until it is set.
- The Google login at the review page is the only point where the guest becomes identifiable, and that is their own choice.

> **Policy reminder:** Strict gating (inviting only 5-star guests) conflicts with Google's review-solicitation policy. This was a conscious decision. The risk is that Google may remove reviews or take action if it is detected.

---

## 7. Alerts

- **Trigger:** overall rating of **3 or below** only. Tags alone do not trigger an alert.
- **Channel:** email only.
- **Content:** property, date/time, rating, selected tags, comments, and the guest's language.
- **Translation:** comments not written in English are **automatically translated to English**, with the translation shown below the original in the email.

---

## 8. Data collected per response

| Field | Source |
|---|---|
| Property | QR link |
| Date/time | Automatic |
| Language | Automatic |
| Overall rating (1-5) | Guest |
| Selected tags per category | Guest, optional |
| Comments per category / general comment | Guest, optional |
| English translation of comments (non-English only) | Automatic, optional |

No personal data is collected.

---

## 9. Storage and review

- **One Google Sheet**, one row per submission, one column per field above. Filter by property, or optionally use a tab per property.
- **Second tab, "Improvements":** issue → action taken → date fixed, per property.
- **Periodic review (monthly or quarterly):** count the most frequent tags per property, then read the comments.
- **Alert-driven handling:** act first on low-score emails.

---

## 10. Implementation outline (Netlify)

Conceptual only, not a build plan.

- **Front end:** one static page on Netlify. Property (from `?p=`), language detection and form logic run in the page. Translations are kept in a simple file per language.
- **Back end:** one **Netlify serverless function** that receives the submission and:
  1. validates it and applies spam protection,
  2. appends a row to the Google Sheet (Google Sheets API with a service account),
  3. if the rating is 3 or below, translates non-English comments to English and sends the alert email via a simple email service (e.g. Resend, Mailgun or SendGrid).
- **Spam protection (basic):** a hidden honeypot field in the form, plus basic server-side validation (valid property, rating range, text length limits).
- **Configuration values:** alert email address, Sheet ID, per-property tag lists, and the house Google review URL (to be added later).
- **QR codes:** generated once from the four URLs and printed.
- **Why a function rather than Netlify Forms:** Netlify Forms cannot conditionally email for low ratings only or translate, so a function fits these rules better.

---

## 11. Out of scope

Per-category ratings, stay dates, contact details or consent, check-in or timed messages, sending links, Net Promoter, photo upload, trend dashboards, cross-platform review checks, ranking or public display of ratings.

---

## 12. Decisions summary

| Topic | Decision |
|---|---|
| Rating style | 5 stars |
| Categories | 6 comment topics, no per-category rating |
| Tags | Simple example tags, per-property lists |
| Languages | EN, SK, ES, FR, DE, auto-detected with selector |
| Alerts | Email only, rating 3 or below, translated to English |
| Google review | House only, 5 stars only (strict gating), URL to be added later |
| Storage | Google Sheet |
| Spam protection | Basic (honeypot + validation) |
| Hosting | Netlify |
| QR codes | One per property, same code at 2-3 locations |

---

## 13. Open items

1. Add the **Google review URL** for the house (later).
2. Confirm the **alert email address**.
3. Final wording of the form in the five languages.
4. Choose the email and translation service for the function.

**Suggested next steps:** (1) draft the form wording in all five languages, (2) technical build plan for the Netlify project.
