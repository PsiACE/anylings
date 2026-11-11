package domain

import (
	"errors"
	"fmt"
)

// Currency is a three-letter ISO-4217 style code. Only USD is modelled while
// the platform has a single billing currency.
type Currency string

const USD Currency = "USD"

// Money is an integer amount of minor units (cents for USD). Floating point is
// never used for billing arithmetic.
type Money struct {
	Minor    int64
	Currency Currency
}

// USDcents builds a Money value in cents.
func USDcents(c int64) Money { return Money{Minor: c, Currency: USD} }

var (
	ErrCurrencyMismatch = errors.New("domain: currency mismatch")
	ErrNegativeAmount   = errors.New("domain: negative amount")
)

func (m Money) Validate() error {
	if m.Currency != USD {
		return fmt.Errorf("domain: unsupported currency %q", m.Currency)
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

// MonthlyBaseline is the fixed per-workspace planning baseline. It is a budget
// target for reservation accounting, not an enforced provider billing cap.
var MonthlyBaseline = Money{Minor: 2400, Currency: USD}

// ElasticBudgetEnabled reports whether spend above MonthlyBaseline may be
// reserved. Elastic budget is not approved, so it is always false.
const ElasticBudgetEnabled = false
