/// Microsoft Entra (Azure AD) OAuth — **Hash-Sec Tunnel**
///
/// ### End users
/// They only **sign in** in the app. They do **not** configure this file.
///
/// ### Publisher (you)
/// The Azure CLI’s built-in OAuth client is **not allowed for personal
/// Microsoft accounts** (`@outlook.com`, etc.). You register **one** Entra
/// **app** (public Client ID — like any mobile app’s OAuth id) and put it
/// here **before** you ship APKs. That does **not** give users access to your
/// subscription; each user still logs in as **themselves** and only manages
/// **their** Azure resources.
///
/// Steps: [AZURE_PORTAL_ONCE.md](../../AZURE_PORTAL_ONCE.md)

/// **Publisher:** paste your multi-tenant app’s **Application (client) ID**
/// before releasing. Leave empty only for local dev (then use
/// `--dart-define=AZURE_CLIENT_ID=...` or [tool/build_release.sh](../../tool/build_release.sh)).
const String _kEmbeddedAzureClientId = '4ffd3eb8-543c-4799-9f59-4a175dc82f87';

/// Build-time override: `flutter build apk --dart-define=AZURE_CLIENT_ID=...`
const String kAzureOAuthClientId = String.fromEnvironment(
  'AZURE_CLIENT_ID',
  defaultValue: _kEmbeddedAzureClientId,
);

const String kAzureOAuthRedirectUri = 'hamieh://auth';

bool get isAzureOAuthClientConfigured {
  final id = kAzureOAuthClientId.trim();
  return id.length >= 32 && id.contains('-');
}
