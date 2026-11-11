package domain

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
)

// Identifier types are distinct so a task run id cannot be passed where a
// workspace id is expected.
type (
	WorkspaceID   string
	BoxID         string
	TaskRunID     string
	AttemptID     string
	LeaseID       string
	ArtifactID    string
	RequestID     string
	CorrelationID string
)

// ErrInvalidID is returned by Parse helpers for empty or malformed values.
var ErrInvalidID = errors.New("domain: invalid identifier")

// NewID returns a random 128-bit identifier with the given prefix. Identifiers
// are opaque to callers and safe to persist as text.
func NewID(prefix string) string {
	var buf [16]byte
	if _, err := rand.Read(buf[:]); err != nil {
		panic("domain: entropy unavailable: " + err.Error())
	}
	return prefix + "_" + hex.EncodeToString(buf[:])
}

func validID(prefix, value string) error {
	if len(value) != len(prefix)+1+32 || value[:len(prefix)+1] != prefix+"_" {
		return ErrInvalidID
	}
	if _, err := hex.DecodeString(value[len(prefix)+1:]); err != nil {
		return ErrInvalidID
	}
	return nil
}

func (id WorkspaceID) Validate() error { return validID("ws", string(id)) }
func (id BoxID) Validate() error       { return validID("box", string(id)) }
func (id TaskRunID) Validate() error   { return validID("run", string(id)) }
func (id AttemptID) Validate() error   { return validID("att", string(id)) }
func (id LeaseID) Validate() error     { return validID("lease", string(id)) }
func (id ArtifactID) Validate() error  { return validID("art", string(id)) }
