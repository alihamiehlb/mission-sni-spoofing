import 'package:flutter/material.dart';

class AppTheme {
  static const primary = Color(0xFF00E5FF);
  static const primaryDark = Color(0xFF0091EA);
  static const accent = Color(0xFFAB47BC);
  static const success = Color(0xFF00E676);
  static const danger = Color(0xFFFF1744);
  static const warning = Color(0xFFFFAB00);
  static const surface = Color(0xFF080B14);
  static const surfaceCard = Color(0xFF111827);
  static const surfaceElevated = Color(0xFF1E293B);
  static const azure = Color(0xFF0078D4);

  static const _gradient = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFF0D47A1), Color(0xFF00BCD4)],
  );

  static LinearGradient get gradient => _gradient;

  static ThemeData dark() {
    return ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      colorScheme: const ColorScheme.dark(
        primary: primary,
        secondary: accent,
        surface: surface,
        error: danger,
      ),
      scaffoldBackgroundColor: surface,
      cardColor: surfaceCard,
      appBarTheme: const AppBarTheme(
        backgroundColor: Colors.transparent,
        elevation: 0,
        centerTitle: true,
        titleTextStyle: TextStyle(
          fontSize: 18,
          fontWeight: FontWeight.w700,
          color: Colors.white,
          letterSpacing: -0.3,
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: primary,
          foregroundColor: Colors.black,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
          padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 16),
          textStyle: const TextStyle(fontWeight: FontWeight.w700, fontSize: 16),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: surfaceElevated,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: BorderSide.none,
        ),
        contentPadding: const EdgeInsets.symmetric(horizontal: 18, vertical: 16),
        labelStyle: const TextStyle(color: Colors.white54),
      ),
    );
  }
}
