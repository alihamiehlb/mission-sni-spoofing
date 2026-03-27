// Package auth provides token-based authentication for the relay server.
// Supports both JWT (HS256) and plain shared-secret tokens.
package auth

import (
	"crypto/subtle"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// Authenticator validates incoming client tokens.
type Authenticator struct {
	secret []byte
}

// New creates an Authenticator with the given secret.
func New(secret string) *Authenticator {
	return &Authenticator{secret: []byte(secret)}
}

// Verify returns true if token is a valid JWT or matches the raw secret.
func (a *Authenticator) Verify(token string) bool {
	if token == "" {
		return false
	}

	// Try JWT first
	parsed, err := jwt.Parse(token, func(t *jwt.Token) (interface{}, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, jwt.ErrSignatureInvalid
		}
		return a.secret, nil
	}, jwt.WithExpirationRequired())

	if err == nil && parsed.Valid {
		return true
	}

	// Fall back to constant-time raw secret comparison
	// This allows using the raw secret as a simple token without JWT
	return subtle.ConstantTimeCompare([]byte(token), a.secret) == 1
}

// GenerateToken creates a signed JWT valid for ttl duration.
func (a *Authenticator) GenerateToken(ttl time.Duration) (string, error) {
	claims := jwt.MapClaims{
		"iat": time.Now().Unix(),
		"exp": time.Now().Add(ttl).Unix(),
		"sub": "hamieh-client",
	}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	return token.SignedString(a.secret)
}
