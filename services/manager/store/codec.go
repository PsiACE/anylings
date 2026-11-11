package store

import (
	"database/sql"
	"fmt"
	"strings"
	"time"

	"github.com/PsiACE/anylings/services/manager/domain"
)

func formatTime(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }

func scanTime(s string) (time.Time, error) {
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}, fmt.Errorf("store: parse timestamp %q: %w", s, err)
	}
	return t.UTC(), nil
}

func nullTime(s sql.NullString) (*time.Time, error) {
	if !s.Valid {
		return nil, nil
	}
	t, err := scanTime(s.String)
	if err != nil {
		return nil, err
	}
	return &t, nil
}

func timeValue(t *time.Time) any {
	if t == nil {
		return nil
	}
	return formatTime(*t)
}

// mapWriteError turns a uniqueness violation into a stable domain error.
// Callers branch on the value; no driver text is parsed elsewhere.
//
// Only uniqueness is mapped. A foreign-key or check violation is a real bug in
// the caller's data and must surface as itself, not as a duplicate request.
func mapWriteError(err error) error {
	if err == nil {
		return nil
	}
	msg := strings.ToLower(err.Error())
	if strings.Contains(msg, "unique constraint") {
		return fmt.Errorf("%w: %v", domain.ErrConflict, err)
	}
	return err
}
