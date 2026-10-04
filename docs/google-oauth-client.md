# Creating your Google OAuth client

gdrive-od talks to Google Drive through rclone, and rclone needs an *OAuth client* to sign in to
your Google account. rclone used to ship a shared one, but that shared client is retired during
2026, so you need your own. It is free and takes about five minutes. **One client serves all your
Google accounts**, on all your computers.

You will end up with a small JSON file (`client_secret_….json`). `install.sh` and
`gdrive-od add-account` ask for it.

## 1. Create a project

1. Open <https://console.cloud.google.com/> and sign in with any of your Google accounts. A
   personal Gmail account is the simplest choice; university or company accounts may not be
   allowed to create projects.
2. Create a project: <https://console.cloud.google.com/projectcreate>. Name it, for example,
   `gdrive-od`, and click **Create**.
3. Make sure the new project is selected in the project picker at the top of the page.

## 2. Enable the Google Drive API

1. Open <https://console.cloud.google.com/apis/library/drive.googleapis.com>.
2. Click **Enable**.

## 3. Configure the consent screen

1. Open **Google Auth Platform**: <https://console.cloud.google.com/auth/overview>, and click
   **Get started**.
2. **App information:** app name `gdrive-od`. For the user support email, pick your address.
3. **Audience:** choose **External**. "Internal" is only possible if the project belongs to a
   Google Workspace organisation and every account you'll sync is in that organisation.
4. **Contact information:** your email address.
5. **Finish:** accept the *Google API Services User Data Policy*, then click **Create**.

## 4. Allow access to Drive

1. In Google Auth Platform, open **Data access**, then **Add or remove scopes**.
2. Filter for `auth/drive`. Tick the row **`.../auth/drive`** ("See, edit, create and delete all
   of your Google Drive files"), then click **Update**.
3. Click **Save** at the bottom of the Data access page.

## 5. Create the client

1. Open **Clients**, then **Create client**.
2. **Application type:** **Desktop app**. Name it, for example, `gdrive-od`.
3. Click **Create**, then **Download JSON**. Keep that file: the secret is shown only once. (If you
   lose it, add a new secret to the client.)

## 6. Who may sign in: Testing or published

New apps start in **Testing** mode:

- Only the accounts listed as **test users** can sign in. Open **Audience**, then **Test users**,
  then **Add users**, and add **every Google account you want to sync**.
- Google ends the sign-in of a Testing app **every 7 days**. gdrive-od then shows a notification;
  reconnect in one click with right-click › Google Drive › **Reconnect account**, the app's
  **Reconnect…** button, or `gdrive-od reconnect NAME`.

To avoid the weekly sign-in, click **Publish app** under **Audience**. Google only allows this
once the **Branding** page has an application home page and a privacy policy hosted on a domain
you own. Google doesn't need to verify an app for personal use: when signing in you will see
"Google hasn't verified this app". Click **Advanced** and then **Go to gdrive-od** to continue.

## 7. Use it

```bash
gdrive-od add-account personal --email you@gmail.com --client ~/Downloads/client_secret_….json
```

The first time, the file is copied to `~/.config/gdrive-ondemand/oauth-client.json` (readable only
by you), so later accounts don't need `--client`.

## Troubleshooting

| What you see | Why / what to do |
|---|---|
| "Access blocked: gdrive-od has not completed the Google verification process" | The account isn't a test user: add it (step 6). |
| "Access blocked: … admin" / "This app is blocked" | A Google Workspace admin (school, company) restricts outside apps. Ask them to allow the client ID, or use a project inside that organisation with an *Internal* audience. |
| "Google hasn't verified this app" | Expected for your own client: Advanced › Go to gdrive-od. |
| The consent page doesn't list Drive access | The scope wasn't saved (step 4), or you didn't tick the checkbox on the consent page. |
| `accessNotConfigured` / "Drive API has not been used in project" | Enable the Google Drive API (step 2) in the same project as the client. |
| Sync stops after a week with `invalid_grant` | Testing-mode expiry: reconnect (step 6). |
