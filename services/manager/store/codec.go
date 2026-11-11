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

// mapWriteError turns SQLite constraint failures into a stable domain error.
// Callers branch on the value; no driver text is parsed elsewhere.
func mapWriteError(err error) error {
	if err == nil {
		return nil
	}
	msg := strings.ToLower(err.Error())
	if strings.Contains(msg, "unique constraint") || strings.Contains(msg, "constraint failed") {
		return fmt.Errorf("%w: %v", domain.ErrConflict, err)
	}
	return err
}
