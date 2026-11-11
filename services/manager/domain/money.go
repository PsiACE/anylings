package domain

import (
	"errors"
	"fmt"
)

// Currency is an upper-case three-letter currency code. The domain only does
// integer arithmetic on minor units; it carries no notion of a price list.
type Currency string

// USD is a currency code constant. It names a unit of account; it does not
// carry a price list or a budget.
const USD Currency = "USD"

// Money is an integer amount of minor units (for example cents). Floating
// point is never used for accounting arithmetic.
type Money struct {
	Minor    int64
	Currency Currency
}

// Amount builds a Money value in minor units of the given currency.
func Amount(minor int64, currency Currency) Money {
	return Money{Minor: minor, Currency: currency}
}

var (
	ErrCurrencyMismatch = errors.New("domain: currency mismatch")
	ErrNegativeAmount   = errors.New("domain: negative amount")
)

func (m Money) Validate() error {
	if l := len(m.Currency); l != 3 {
		return fmt.Errorf("domain: invalid currency %q", m.Currency)
	}
	for i := 0; i < len(m.Currency); i++ {
		if m.Currency[i] < 'A' || m.Currency[i] > 'Z' {
			return fmt.Errorf("domain: invalid currency %q", m.Currency)
		}
	}
	if m.Minor < 0 {
		return ErrNegativeAmount
	}
	return nil
}

func (m Money) Add(n Money) (Money, error) {
	if m.Currency != n.Currency {
		return Money{}, ErrCurrencyMismatch
	}
	return Money{Minor: m.Minor + n.Minor, Currency: m.Currency}, nil
}

func (m Money) Sub(n Money) (Money, error) {
	if m.Currency != n.Currency {
		return Money{}, ErrCurrencyMismatch
	}
	if n.Minor > m.Minor {
		return Money{}, ErrNegativeAmount
	}
	return Money{Minor: m.Minor - n.Minor, Currency: m.Currency}, nil
}

// Exceeds reports whether m is larger than limit in the same currency.
func (m Money) Exceeds(limit Money) (bool, error) {
	if m.Currency != limit.Currency {
		return false, ErrCurrencyMismatch
	}
	return m.Minor > limit.Minor, nil
}
