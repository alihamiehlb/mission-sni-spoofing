# Azure: publisher setup vs what users do

## End users (same idea as the old `start.sh` + `az login`)

They **do not** open Azure Portal for app registration. They:

1. Install your APK.
2. Tap **Sign in with Microsoft** (browser login — same kind of step as `az login`).
3. Use **their own** Microsoft account and **their own** Azure subscription (e.g. [free account](https://azure.microsoft.com/free/) with card verification, like signing up for Azure the first time).
4. **Create tunnel** → the app creates a VM in **their** subscription only.

Nobody else gets access to their Azure. Each person only ever touches **their** resources.

---

## What an “App registration” actually is (not “my Azure for everyone”)

Microsoft requires every OAuth app to have an **Application (client) ID** — a **public** identifier, like a “Facebook App ID” on a Login with Facebook button.

- **One** registration for **Hash-Sec Tunnel** (you, the publisher, do this **once**).
- You **embed that Client ID in the APK** you ship (or in your GitHub release build).
- **All users** share the same Client ID in the binary — that is normal and **does not** mean they use your subscription. They still sign in as **themselves**; tokens are **per user** and only allow ARM actions **that user** is allowed to do on **their** tenant/subscriptions.

The old repo used **`az login`**, which used Microsoft’s **Azure CLI** app ID. That built-in app is **blocked for personal (`@outlook.com`) accounts**, which is why we need **your own** app registration — still **one** ID for the whole product, not one per user.

---

## Checklist — **app publisher only** (you, once)

Do this in a browser when you prepare a release (or the first time you build a distributable APK):

1. Open **[Azure Portal](https://portal.azure.com)** and sign in.

2. **Microsoft Entra ID** → **App registrations** → **New registration**.

3. **Name:** e.g. `Hash-Sec Tunnel`.

4. **Supported account types:**  
   **Accounts in any organizational directory and personal Microsoft accounts**  
   (so consumer emails work, like the old flow for personal Microsoft accounts).

5. **Redirect URI:** platform **Mobile and desktop applications** → **`hamieh://auth`** → **Register**.

6. **Authentication** → **Allow public client flows** → **Yes** → **Save**.

7. **API permissions** → **Add** → **APIs my organization uses** → **Azure Service Management** → Delegated **`user_impersonation`** → **Add**.

8. **Overview** → copy **Application (client) ID**.

9. Put that GUID into the **source** before you build APKs you give to others:
   - `lib/config/oauth_config.dart` → `_kEmbeddedAzureClientId = '...'`, **or**
   - `./tool/build_release.sh ...`

After that, **end users** only install the APK and sign in — **no** Portal steps for them.

---

## Summary

| Who | What they do |
|-----|----------------|
| **You (publisher)** | One Entra app registration + bake Client ID into released APKs. |
| **Each user** | Install app → Microsoft login → own Azure subscription → Create tunnel in **their** account only. |

Same **logic** as the old repo: user identity + user’s Azure; only difference is we can’t reuse the Azure CLI’s OAuth app for personal accounts, so **you** register the app identity once for Hash-Sec Tunnel.
