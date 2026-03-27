import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'services/api_service.dart';
import 'services/azure_service.dart';
import 'screens/login_screen.dart';
import 'screens/home_screen.dart';
import 'theme/app_theme.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  await SystemChrome.setPreferredOrientations([
    DeviceOrientation.portraitUp,
    DeviceOrientation.portraitDown,
  ]);

  SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(
    statusBarColor: Colors.transparent,
    statusBarIconBrightness: Brightness.light,
    systemNavigationBarColor: AppTheme.surface,
    systemNavigationBarIconBrightness: Brightness.light,
  ));

  final api = await ApiService.load();

  runApp(
    ChangeNotifierProvider(
      create: (_) => AppState(api),
      child: const HashSecApp(),
    ),
  );
}

class AppState extends ChangeNotifier {
  final ApiService api;
  AzureService? _azure;
  bool _loggedIn;
  String _pkceVerifier = '';
  int _oauthFailNonce = 0;

  AppState(this.api) : _loggedIn = api.isLoggedIn {
    if (_loggedIn && api.accessToken.isNotEmpty) {
      _azure = AzureService(api.accessToken);
    }
  }

  bool get isLoggedIn => _loggedIn;
  AzureService? get azure => _azure;
  int get oauthFailNonce => _oauthFailNonce;

  void setPkceVerifier(String v) => _pkceVerifier = v;

  void _signalOAuthFailed() {
    _oauthFailNonce++;
    notifyListeners();
  }

  /// Validates OAuth [state] (CSRF) and PKCE, then exchanges [code] for tokens.
  Future<bool> handleAuthCode(String code, {String? state}) async {
    final stateOk = await api.verifyAndClearOAuthState(state);
    if (!stateOk) {
      _pkceVerifier = '';
      _signalOAuthFailed();
      return false;
    }
    if (_pkceVerifier.isEmpty) {
      _signalOAuthFailed();
      return false;
    }
    final ok = await api.exchangeCode(code, _pkceVerifier);
    _pkceVerifier = '';
    if (ok) {
      _azure = AzureService(api.accessToken);
      _loggedIn = true;
      notifyListeners();
      return true;
    }
    _signalOAuthFailed();
    return false;
  }

  Future<void> ensureValidToken() async {
    final token = await api.getValidToken();
    if (token.isNotEmpty) {
      _azure?.updateToken(token);
    }
  }

  Future<void> logout() async {
    await api.logout();
    final az = _azure;
    if (az != null) await az.clearVmInfo();
    _azure = null;
    _loggedIn = false;
    notifyListeners();
  }

  void setLoggedIn(bool v) {
    _loggedIn = v;
    notifyListeners();
  }
}

class HashSecApp extends StatefulWidget {
  const HashSecApp({super.key});

  @override
  State<HashSecApp> createState() => _HashSecAppState();
}

class _HashSecAppState extends State<HashSecApp> {
  static const _channel = MethodChannel('com.hamieh.tunnel/auth');
  final GlobalKey<ScaffoldMessengerState> _messengerKey =
      GlobalKey<ScaffoldMessengerState>();

  @override
  void initState() {
    super.initState();
    _channel.setMethodCallHandler(_handleMethod);
  }

  Future<dynamic> _handleMethod(MethodCall call) async {
    if (call.method != 'onAuthCallback') return;
    final raw = call.arguments;
    if (raw is! Map) return;
    final map = Map<String, dynamic>.from(raw);
    final code = map['code'] as String?;
    final state = map['state'] as String?;
    if (code == null || code.isEmpty) return;

    final appState = context.read<AppState>();
    final ok = await appState.handleAuthCode(code, state: state);
    if (!ok && mounted) {
      _messengerKey.currentState?.showSnackBar(
        const SnackBar(
          content: Text(
            'Sign-in could not be verified. Close this message and try again.',
          ),
          behavior: SnackBarBehavior.floating,
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Hash-Sec Tunnel',
      debugShowCheckedModeBanner: false,
      scaffoldMessengerKey: _messengerKey,
      theme: AppTheme.dark(),
      home: Consumer<AppState>(
        builder: (ctx, state, _) =>
            state.isLoggedIn ? const HomeScreen() : const LoginScreen(),
      ),
    );
  }
}
