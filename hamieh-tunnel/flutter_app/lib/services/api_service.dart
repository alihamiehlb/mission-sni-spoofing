import 'dart:convert';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import '../config/oauth_config.dart';

class ApiService {
  static const _secureStorage = FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
  );

  static const _accessTokenKey = 'hashsec_access_token';
  static const _refreshTokenKey = 'hashsec_refresh_token';
  static const _tokenExpiryKey = 'hashsec_token_expiry';
  static const _relayTokenKey = 'hashsec_relay_token';
  static const _vmInfoKey = 'hashsec_vm_info';
  static const _oauthStateKey = 'hashsec_oauth_state';

  // Non-sensitive data stays in SharedPreferences
  static const _usageKey = 'hashsec_daily_usage_mb';
  static const _usageDateKey = 'hashsec_usage_date';

  static String get _clientId => kAzureOAuthClientId;
  static String get _redirectUri => kAzureOAuthRedirectUri;
  static const _tokenUrl =
      'https://login.microsoftonline.com/common/oauth2/v2.0/token';
  static const _scope =
      'https://management.azure.com/.default offline_access openid profile';

  String _accessToken;
  String _refreshToken;
  DateTime _tokenExpiry;

  ApiService({
    String accessToken = '',
    String refreshToken = '',
    DateTime? tokenExpiry,
  })  : _accessToken = accessToken,
        _refreshToken = refreshToken,
        _tokenExpiry = tokenExpiry ?? DateTime(2000);

  String get accessToken => _accessToken;
  bool get isLoggedIn => _refreshToken.isNotEmpty;
  bool get isTokenExpired => DateTime.now().isAfter(_tokenExpiry);

  static Future<ApiService> load() async {
    final accessToken = await _secureStorage.read(key: _accessTokenKey) ?? '';
    final refreshToken = await _secureStorage.read(key: _refreshTokenKey) ?? '';
    final expiryStr = await _secureStorage.read(key: _tokenExpiryKey) ?? '0';
    final expMs = int.tryParse(expiryStr) ?? 0;

    return ApiService(
      accessToken: accessToken,
      refreshToken: refreshToken,
      tokenExpiry: DateTime.fromMillisecondsSinceEpoch(expMs),
    );
  }

  Future<bool> exchangeCode(String code, String codeVerifier) async {
    try {
      final resp = await http.post(
        Uri.parse(_tokenUrl),
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: {
          'client_id': _clientId,
          'grant_type': 'authorization_code',
          'code': code,
          'redirect_uri': _redirectUri,
          'code_verifier': codeVerifier,
          'scope': _scope,
        },
      );
      if (resp.statusCode != 200) return false;
      return await _processTokenResponse(jsonDecode(resp.body));
    } catch (_) {
      return false;
    }
  }

  Future<bool> refreshAccessToken() async {
    if (_refreshToken.isEmpty) return false;
    try {
      final resp = await http.post(
        Uri.parse(_tokenUrl),
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: {
          'client_id': _clientId,
          'grant_type': 'refresh_token',
          'refresh_token': _refreshToken,
          'scope': _scope,
        },
      );
      if (resp.statusCode != 200) return false;
      return await _processTokenResponse(jsonDecode(resp.body));
    } catch (_) {
      return false;
    }
  }

  Future<String> getValidToken() async {
    if (!isTokenExpired && _accessToken.isNotEmpty) return _accessToken;
    if (await refreshAccessToken()) return _accessToken;
    return '';
  }

  Future<bool> _processTokenResponse(Map<String, dynamic> data) async {
    final at = data['access_token'] as String?;
    final rt = data['refresh_token'] as String?;
    final expiresIn = data['expires_in'] as int? ?? 3600;

    if (at == null) return false;

    _accessToken = at;
    if (rt != null) _refreshToken = rt;
    _tokenExpiry = DateTime.now().add(Duration(seconds: expiresIn - 120));

    await _saveTokens();
    return true;
  }

  /// CSRF protection for OAuth redirect: must match value returned by IdP.
  Future<void> storePendingOAuthState(String state) async {
    await _secureStorage.write(key: _oauthStateKey, value: state);
  }

  /// Returns true if [returnedState] matches the pending value (one-time use).
  Future<bool> verifyAndClearOAuthState(String? returnedState) async {
    final expected = await _secureStorage.read(key: _oauthStateKey);
    await _secureStorage.delete(key: _oauthStateKey);
    if (expected == null || expected.isEmpty) return false;
    if (returnedState == null || returnedState.isEmpty) return false;
    return expected == returnedState;
  }

  Future<void> _saveTokens() async {
    await _secureStorage.write(key: _accessTokenKey, value: _accessToken);
    await _secureStorage.write(key: _refreshTokenKey, value: _refreshToken);
    await _secureStorage.write(
      key: _tokenExpiryKey,
      value: _tokenExpiry.millisecondsSinceEpoch.toString(),
    );
  }

  Future<void> logout() async {
    _accessToken = '';
    _refreshToken = '';
    _tokenExpiry = DateTime(2000);

    await _secureStorage.delete(key: _accessTokenKey);
    await _secureStorage.delete(key: _refreshTokenKey);
    await _secureStorage.delete(key: _tokenExpiryKey);
    await _secureStorage.delete(key: _relayTokenKey);
    await _secureStorage.delete(key: _vmInfoKey);
    await _secureStorage.delete(key: _oauthStateKey);

    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_usageKey);
    await prefs.remove(_usageDateKey);
  }

  // -- Secure storage helpers for AzureService --

  static Future<void> secureWrite(String key, String value) async {
    await _secureStorage.write(key: key, value: value);
  }

  static Future<String?> secureRead(String key) async {
    return await _secureStorage.read(key: key);
  }

  static Future<void> secureDelete(String key) async {
    await _secureStorage.delete(key: key);
  }

  // -- Usage tracking (non-sensitive, SharedPreferences is fine) --

  Future<double> getTodayUsageMb() async {
    final prefs = await SharedPreferences.getInstance();
    final dateStr = prefs.getString(_usageDateKey) ?? '';
    final today = DateTime.now().toIso8601String().substring(0, 10);
    if (dateStr != today) {
      await prefs.setString(_usageDateKey, today);
      await prefs.setDouble(_usageKey, 0.0);
      return 0.0;
    }
    return prefs.getDouble(_usageKey) ?? 0.0;
  }

  Future<void> updateUsageMb(double mb) async {
    final prefs = await SharedPreferences.getInstance();
    final today = DateTime.now().toIso8601String().substring(0, 10);
    await prefs.setString(_usageDateKey, today);
    await prefs.setDouble(_usageKey, mb);
  }

  void dispose() {}
}
