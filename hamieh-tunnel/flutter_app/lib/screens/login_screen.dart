import 'dart:convert';
import 'dart:math';
import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';
import '../config/oauth_config.dart';
import '../main.dart';
import '../theme/app_theme.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen>
    with SingleTickerProviderStateMixin {
  bool _loading = false;
  String? _error;
  late AnimationController _bgCtrl;
  int _lastOauthFailNonce = 0;

  static const _scope =
      'https://management.azure.com/.default offline_access openid profile';
  static const _tenant = 'common';

  @override
  void initState() {
    super.initState();
    _bgCtrl = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 8),
    )..repeat();
  }

  @override
  void dispose() {
    _bgCtrl.dispose();
    super.dispose();
  }

  String _generateCodeVerifier() {
    final rng = Random.secure();
    final bytes = List.generate(32, (_) => rng.nextInt(256));
    return base64Url.encode(bytes).replaceAll('=', '');
  }

  String _generateCodeChallenge(String verifier) {
    final digest = sha256.convert(utf8.encode(verifier));
    return base64Url.encode(digest.bytes).replaceAll('=', '');
  }

  Future<void> _signIn() async {
    setState(() {
      _loading = true;
      _error = null;
    });

    if (!isAzureOAuthClientConfigured) {
      setState(() {
        _error =
            'Azure Client ID is not set. Add it in lib/config/oauth_config.dart '
            'or build with --dart-define=AZURE_CLIENT_ID=... (see project README).';
        _loading = false;
      });
      return;
    }

    try {
      final codeVerifier = _generateCodeVerifier();
      final codeChallenge = _generateCodeChallenge(codeVerifier);

      final appState = context.read<AppState>();
      appState.setPkceVerifier(codeVerifier);

      final state = base64Url
          .encode(List.generate(16, (_) => Random.secure().nextInt(256)))
          .replaceAll('=', '');

      await context.read<AppState>().api.storePendingOAuthState(state);

      final authUrl = Uri.parse(
        'https://login.microsoftonline.com/$_tenant/oauth2/v2.0/authorize'
        '?client_id=${Uri.encodeComponent(kAzureOAuthClientId)}'
        '&response_type=code'
        '&redirect_uri=${Uri.encodeComponent(kAzureOAuthRedirectUri)}'
        '&scope=${Uri.encodeComponent(_scope)}'
        '&state=$state'
        '&code_challenge=$codeChallenge'
        '&code_challenge_method=S256'
        '&prompt=select_account',
      );

      final launched = await launchUrl(
        authUrl,
        mode: LaunchMode.externalApplication,
      );

      if (!launched) {
        setState(() {
          _error = 'Could not open browser';
          _loading = false;
        });
      }
      // Browser opens; the deep link handler in MainActivity will
      // catch the redirect and call AppState.handleAuthCode()
    } catch (e) {
      setState(() {
        _error = 'Sign-in failed: $e';
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final oauthFails = context.watch<AppState>().oauthFailNonce;
    if (oauthFails != _lastOauthFailNonce) {
      _lastOauthFailNonce = oauthFails;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) setState(() => _loading = false);
      });
    }

    return Scaffold(
      body: AnimatedBuilder(
        animation: _bgCtrl,
        builder: (context, child) {
          return Container(
            decoration: BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment(
                  sin(_bgCtrl.value * 2 * pi) * 0.5,
                  cos(_bgCtrl.value * 2 * pi) * 0.5,
                ),
                end: Alignment(
                  cos(_bgCtrl.value * 2 * pi) * 0.5 + 0.5,
                  sin(_bgCtrl.value * 2 * pi) * 0.5 + 0.5,
                ),
                colors: const [
                  Color(0xFF080B14),
                  Color(0xFF0D1B2A),
                  Color(0xFF0A192F),
                  Color(0xFF080B14),
                ],
                stops: const [0.0, 0.3, 0.7, 1.0],
              ),
            ),
            child: child,
          );
        },
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 32),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  const SizedBox(height: 40),

                  Container(
                    width: 120,
                    height: 120,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      gradient: LinearGradient(
                        colors: [
                          AppTheme.primary.withAlpha(80),
                          AppTheme.azure.withAlpha(40),
                        ],
                      ),
                      border: Border.all(
                        color: AppTheme.primary.withAlpha(100),
                        width: 2,
                      ),
                      boxShadow: [
                        BoxShadow(
                          color: AppTheme.primary.withAlpha(50),
                          blurRadius: 50,
                          spreadRadius: 8,
                        ),
                      ],
                    ),
                    child: const Icon(
                      Icons.shield_rounded,
                      size: 56,
                      color: AppTheme.primary,
                    ),
                  ).animate().scale(
                        duration: 800.ms,
                        curve: Curves.elasticOut,
                      ),
                  const SizedBox(height: 32),

                  const Text(
                    'HASH-SEC',
                    style: TextStyle(
                      fontSize: 34,
                      fontWeight: FontWeight.w900,
                      letterSpacing: 6,
                      color: Colors.white,
                    ),
                  ).animate().fadeIn(duration: 600.ms, delay: 200.ms),

                  ShaderMask(
                    shaderCallback: (bounds) => const LinearGradient(
                      colors: [AppTheme.primary, AppTheme.azure],
                    ).createShader(bounds),
                    child: const Text(
                      'TUNNEL',
                      style: TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w600,
                        letterSpacing: 12,
                        color: Colors.white,
                      ),
                    ),
                  ).animate().fadeIn(duration: 600.ms, delay: 400.ms),

                  const SizedBox(height: 16),
                  Text(
                    'Free 20 GB  •  7:30 AM – 2:00 PM',
                    style: TextStyle(
                      color: AppTheme.primary.withAlpha(180),
                      fontSize: 14,
                      fontWeight: FontWeight.w500,
                    ),
                  ).animate().fadeIn(duration: 600.ms, delay: 600.ms),

                  const SizedBox(height: 56),

                  // Microsoft sign-in button
                  SizedBox(
                    width: double.infinity,
                    height: 56,
                    child: ElevatedButton(
                      onPressed: _loading ? null : _signIn,
                      style: ElevatedButton.styleFrom(
                        backgroundColor: Colors.white,
                        foregroundColor: const Color(0xFF333333),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(14),
                        ),
                        elevation: 0,
                      ),
                      child: _loading
                          ? const SizedBox(
                              width: 24,
                              height: 24,
                              child: CircularProgressIndicator(
                                strokeWidth: 2.5,
                                color: Color(0xFF333333),
                              ),
                            )
                          : Row(
                              mainAxisAlignment: MainAxisAlignment.center,
                              children: [
                                SizedBox(
                                  width: 22,
                                  height: 22,
                                  child: CustomPaint(
                                    painter: _MicrosoftLogoPainter(),
                                  ),
                                ),
                                const SizedBox(width: 14),
                                const Text(
                                  'Sign in with Microsoft',
                                  style: TextStyle(
                                    fontWeight: FontWeight.w600,
                                    fontSize: 16,
                                    color: Color(0xFF333333),
                                  ),
                                ),
                              ],
                            ),
                    ),
                  )
                      .animate()
                      .slideY(begin: 0.15, end: 0, duration: 700.ms, delay: 500.ms, curve: Curves.easeOut)
                      .fadeIn(duration: 700.ms, delay: 500.ms),

                  if (_error != null) ...[
                    const SizedBox(height: 20),
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
                      decoration: BoxDecoration(
                        color: AppTheme.danger.withAlpha(20),
                        borderRadius: BorderRadius.circular(12),
                        border: Border.all(color: AppTheme.danger.withAlpha(60)),
                      ),
                      child: Row(
                        children: [
                          const Icon(Icons.error_outline, color: AppTheme.danger, size: 18),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Text(_error!, style: const TextStyle(color: AppTheme.danger, fontSize: 13)),
                          ),
                        ],
                      ),
                    ),
                  ],

                  const SizedBox(height: 40),

                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
                    decoration: BoxDecoration(
                      color: AppTheme.azure.withAlpha(15),
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(color: AppTheme.azure.withAlpha(30)),
                    ),
                    child: Column(
                      children: [
                        Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Icon(Icons.info_outline, color: AppTheme.azure.withAlpha(180), size: 18),
                            const SizedBox(width: 12),
                            const Expanded(
                              child: Text(
                                'Sign in with your Azure account. The app will create a small VM '
                                'in your subscription to relay your traffic through Microsoft\'s network.',
                                style: TextStyle(color: Colors.white54, fontSize: 12, height: 1.5),
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 10),
                        Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Icon(Icons.money_off_rounded, color: AppTheme.success.withAlpha(150), size: 18),
                            const SizedBox(width: 12),
                            const Expanded(
                              child: Text(
                                'Azure free tier includes 12 months of free B1s VM. '
                                'Create a free account at azure.microsoft.com/free',
                                style: TextStyle(color: Colors.white54, fontSize: 12, height: 1.5),
                              ),
                            ),
                          ],
                        ),
                      ],
                    ),
                  ).animate().fadeIn(duration: 600.ms, delay: 800.ms),

                  const SizedBox(height: 50),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _MicrosoftLogoPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final gap = size.width * 0.08;
    final cellW = (size.width - gap) / 2;
    final cellH = (size.height - gap) / 2;
    canvas.drawRect(Rect.fromLTWH(0, 0, cellW, cellH), Paint()..color = const Color(0xFFF25022));
    canvas.drawRect(Rect.fromLTWH(cellW + gap, 0, cellW, cellH), Paint()..color = const Color(0xFF7FBA00));
    canvas.drawRect(Rect.fromLTWH(0, cellH + gap, cellW, cellH), Paint()..color = const Color(0xFF00A4EF));
    canvas.drawRect(Rect.fromLTWH(cellW + gap, cellH + gap, cellW, cellH), Paint()..color = const Color(0xFFFFB900));
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
