# Put FOODBRIDGE online (free, about 20 minutes)

Result: a public https:// link judges can open on any phone. HTTPS also makes the microphone work.

## Before you start
- Free hosts wipe local files when the app restarts or sleeps. FOODBRIDGE therefore re-creates the fictional demo accounts and demo listing every time it wakes (`FOODBRIDGE_AUTOSEED=1`).
- That means anything judges register or publish is lost on the next wake. This is fine for a demo, NOT for a real pilot (a real pilot needs a database that persists).
- Never put real people's data into the free demo.

## Part A: put the code on GitHub (browser only, no commands)
1. Create a free account at github.com.
2. Click New repository, name it `foodbridge`, choose Private or Public, click Create.
3. Click "uploading an existing file". Unzip the project first, then drag the CONTENTS of the `foodbridge` folder (the `app`, `web`, `tests` folders and files such as `render.yaml`) into the page. Do not upload the zip itself and do not upload any `.db` file.
4. Click Commit changes.

## Part B: deploy on Render
1. Create a free account at render.com using "Sign in with GitHub".
2. Click New, then Blueprint, pick your `foodbridge` repository, click Connect.
3. Render reads `render.yaml`. When it asks for `FOODBRIDGE_ADMIN_PASSWORD`, type a strong password of your own and write it down. This is the admin login for the live site.
4. Click Apply and wait for the build (a few minutes). When the status says Live, open the address shown at the top (like `https://foodbridge-xxxx.onrender.com`).
5. Log in as `admin` with your password. Demo logins for the fictional organisations are in the README (`bakery`, `shelter-a`, and so on, password `demo-pass-123`).

If the Blueprint route gives trouble: New, then Web Service, connect the repo, Runtime Python, Build command `pip install -r requirements.txt`, Start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, then add environment variables `FOODBRIDGE_AUTOSEED=1`, `FOODBRIDGE_SECRET=` (any long random text) and `FOODBRIDGE_ADMIN_PASSWORD=` (your password).

## Part C: check it works (do this on your phone)
1. Open the link, log in as `bakery`, go to New listing.
2. Tap Speak and say a sentence (allow the microphone). If the mic does not appear, try Chrome.
3. Press Fill the form, tick the confirmation, publish. Log in as `shelter-a` and claim it.
4. Open Proof and press Verify chain.

## Demo day tips
- The free service sleeps after about 15 minutes with no visitors and takes up to a minute to wake. Open the link 2 minutes before judges do, and click through once.
- Share the admin password privately. Do not put it in the README or the slides.
- Always keep a recorded demo video as backup, and be able to run it locally (README) if the internet fails.

## Not tested here
The Docker file and the Render deployment itself could not be run from the build environment. The same startup (`FOODBRIDGE_AUTOSEED=1`, hosted-style environment variables, a simulated wipe and restart) was tested locally and works. If Render shows an error, copy the red text from its Logs tab and send it to me.
