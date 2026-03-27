# Hash-Sec Tunnel

Android app built on the **mission-sni-spoofing** idea: traffic goes through a **TLS relay** on **Microsoft Azure**, with **ClientHello SNI** set to `teams.microsoft.com`, while the relay forwards to your real destinations.

> **Disclaimer:** For authorized security research and personal use only. Respect carrier and cloud provider terms of service and applicable law.

## How to use the app

1. Install the **Hash-Sec Tunnel** APK on your Android device.
2. Open the app and tap **Sign in with Microsoft** — complete login in the browser.
3. You need a **Microsoft account** with an **Azure subscription** (for example the [free Azure account](https://azure.microsoft.com/free/)); the app only uses **your** subscription to create and manage **your** tunnel VM.
4. Use **Create tunnel** (or the in-app controls) so the app provisions the relay VM in your Azure subscription, then connect when the app shows the tunnel is ready. VPN permission is required so traffic can go through the tunnel.

Your login and Azure resources stay yours; nothing in the flow is meant to give other people access to your account.

## Compared to the original repo

The earlier project was mainly a **CLI / desktop** workflow (`az login`, scripts like `start.sh`, Python tooling). This fork adds:

- A **Flutter Android app** with in-app **Microsoft sign-in** (OAuth with PKCE) instead of relying on the Azure CLI login flow on a PC.
- **Per-user Azure VMs** created from the phone via the Azure REST API — each user runs a relay in **their own** subscription, not a shared server you configure by hand.
- An **on-device VPN** path using Android `VpnService` plus the native **hamieh** / tun2socks stack, so the tunnel is usable directly from the phone.
- A **custom Microsoft Entra (Azure AD) app registration** for login, so personal Microsoft accounts are not blocked the way they often are with the default **Azure CLI** OAuth client.

The original Python package, Go relay, and native client sources still live under [`hamieh-tunnel/`](hamieh-tunnel/) for anyone digging into the codebase.

## Credits

Updates and Android app work on top of the original **mission-sni-spoofing** repository: **Ali Hussein Hamieh** ([@alihamiehlb](https://github.com/alihamiehlb)).
