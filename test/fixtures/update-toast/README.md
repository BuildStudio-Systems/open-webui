# Update notice isolated browser regression

From the frontend checkout, run `node node_modules/vite/bin/vite.js --config test/fixtures/update-toast/vite.config.mjs`.
This listens only on127.0.0.1:18764 and refuses a different port. The fixture imports
the actual UpdateInfoToast component, app CSS and Tailwind source. Only i18n strings,
the build version and surrounding composer are synthetic; it does not authenticate
or contact production. Stop the development server after testing.

Check `/?lang=zh` at390×844, `/?lang=ja` at320×568 and `/?lang=en` at1280×720.
With the notice still visible, verify that its rectangle is above the composer,
the page has no horizontal overflow and the input accepts text. Click the translated
Close button (44×44px minimum): the notice must disappear and input text remain.
No change to production version polling or24-hour dismissal persistence is made.
These are component checks, not a substitute for post-deployment full-page/mobile
keyboard acceptance. The fixture is not an application route or shipped page.
