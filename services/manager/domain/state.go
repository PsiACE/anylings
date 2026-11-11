package domain

import (
	"errors"
	"fmt"
)

// ResourceLifecycle is the state of a persistent, reusable resource such as a
// workspace box. It is deliberately separate from TaskState: reclaiming a box
// says nothing about whether any task run succeeded, and a task run may end
// while its box stays Ready.
type ResourceLifecycle string

const (
	ResourceRequested    ResourceLifecycle = "Requested"
	ResourceProvisioning ResourceLifecycle = "Provisioning"
	ResourceReady        ResourceLifecycle = "Ready"
	ResourceReclaimed    ResourceLifecycle = "Reclaimed"
)

// TaskState describes one task run:
// Requested -> Provisioning -> Ready -> Running -> Collecting -> Terminating
// -> Reclaimed, with Failed and Interrupted as distinct outcomes.
//
// Reclaimed is the successful terminal state: the run finished and its
// ephemeral lease was released. Failed and Interrupted say why a run did not
// reach it. None of these imply the persistent workspace box was deleted; that
// is a separate ResourceLifecycle transition.
type TaskState string

const (
	TaskRequested    TaskState = "Requested"
	TaskProvisioning TaskState = "Provisioning"
	TaskReady        TaskState = "Ready"
	TaskRunning      TaskState = "Running"
	TaskCollecting   TaskState = "Collecting"
	TaskTerminating  TaskState = "Terminating"
	TaskReclaimed    TaskState = "Reclaimed"
	TaskFailed       TaskState = "Failed"
	TaskInterrupted  TaskState = "Interrupted"
)

// AttemptState describes one bounded try at executing a task run.
type AttemptState string

const (
	AttemptStarted   AttemptState = "Started"
	AttemptRunning   AttemptState = "Running"
	AttemptSucceeded AttemptState = "Succeeded"
	AttemptFailed    AttemptState = "Failed"
	AttemptExpired   AttemptState = "Expired"
	AttemptCancelled AttemptState = "Cancelled"
)

// LeaseState describes an ephemeral sandbox lease held by an attempt. Stranded
// records a lease whose release failed and which therefore needs a janitor.
type LeaseState string

const (
	LeaseAllocating LeaseState = "Allocating"
	LeaseActive     LeaseState = "Active"
	LeaseReleasing  LeaseState = "Releasing"
	LeaseReleased   LeaseState = "Released"
	LeaseStranded   LeaseState = "Stranded"
)

var (
	ErrInvalidTransition = errors.New("domain: invalid state transition")
	ErrTerminalState     = errors.New("domain: state is terminal")
)

// resourceTransitions is the single allowed edge set for box lifecycle. There
// is no edge out of Reclaimed, and no path back to Ready from Reclaimed.
var resourceTransitions = map[ResourceLifecycle][]ResourceLifecycle{
	ResourceRequested:    {ResourceProvisioning, ResourceReclaimed},
	ResourceProvisioning: {ResourceReady, ResourceReclaimed},
	ResourceReady:        {ResourceReclaimed},
	ResourceReclaimed:    nil,
}

var taskTransitions = map[TaskState][]TaskState{
	TaskRequested:    {TaskProvisioning, TaskTerminating, TaskFailed, TaskInterrupted},
	TaskProvisioning: {TaskReady, TaskTerminating, TaskFailed, TaskInterrupted},
	TaskReady:        {TaskRunning, TaskTerminating, TaskFailed, TaskInterrupted},
	TaskRunning:      {TaskCollecting, TaskTerminating, TaskFailed, TaskInterrupted},
	TaskCollecting:   {TaskTerminating, TaskFailed, TaskInterrupted},
	TaskTerminating:  {TaskReclaimed, TaskFailed, TaskInterrupted},
	TaskReclaimed:    nil,
	TaskFailed:       nil,
	TaskInterrupted:  nil,
}

var attemptTransitions = map[AttemptState][]AttemptState{
	AttemptStarted:   {AttemptRunning, AttemptFailed, AttemptCancelled, AttemptExpired},
	AttemptRunning:   {AttemptSucceeded, AttemptFailed, AttemptCancelled, AttemptExpired},
	AttemptSucceeded: nil,
	AttemptFailed:    nil,
	AttemptExpired:   nil,
	AttemptCancelled: nil,
}

var leaseTransitions = map[LeaseState][]LeaseState{
	LeaseAllocating: {LeaseActive, LeaseReleased, LeaseStranded},
	LeaseActive:     {LeaseReleasing, LeaseStranded},
	LeaseReleasing:  {LeaseReleased, LeaseStranded},
	LeaseReleased:   nil,
	LeaseStranded:   {LeaseReleasing, LeaseReleased},
}

func canTransition[T ~string](edges map[T][]T, from, to T) bool {
	for _, candidate := range edges[from] {
		if candidate == to {
			return true
		}
	}
	return false
}

// CanTransition reports whether the state machine allows from -> to.
func (from ResourceLifecycle) CanTransition(to ResourceLifecycle) bool {
	return canTransition(resourceTransitions, from, to)
}
func (from TaskState) CanTransition(to TaskState) bool {
	return canTransition(taskTransitions, from, to)
}
func (from AttemptState) CanTransition(to AttemptState) bool {
	return canTransition(attemptTransitions, from, to)
}
func (from LeaseState) CanTransition(to LeaseState) bool {
	return canTransition(leaseTransitions, from, to)
}

func (s ResourceLifecycle) Terminal() bool { return s == ResourceReclaimed }

func (s TaskState) Terminal() bool {
	switch s {
	case TaskReclaimed, TaskFailed, TaskInterrupted:
		return true
	}
	return false
}

func (s AttemptState) Terminal() bool {
	switch s {
	case AttemptSucceeded, AttemptFailed, AttemptExpired, AttemptCancelled:
		return true
	}
	return false
}

func (s LeaseState) Terminal() bool { return s == LeaseReleased }

// TransitionError carries the rejected edge so it can be audited. It never
// contains secrets or prompt content.
type TransitionError struct {
	Kind     string
	From, To string
}

func (e TransitionError) Error() string {
	return fmt.Sprintf("domain: invalid %s transition %s -> %s", e.Kind, e.From, e.To)
}

func (e TransitionError) Is(target error) bool { return target == ErrInvalidTransition }

func resourceEdge(from, to ResourceLifecycle) error {
	return TransitionError{Kind: "resource", From: string(from), To: string(to)}
}
func taskEdge(from, to TaskState) error {
	return TransitionError{Kind: "task", From: string(from), To: string(to)}
}
func attemptEdge(from, to AttemptState) error {
	return TransitionError{Kind: "attempt", From: string(from), To: string(to)}
}
func leaseEdge(from, to LeaseState) error {
	return TransitionError{Kind: "lease", From: string(from), To: string(to)}
}

// Transition validates the edge and returns a typed error when it is rejected.
func (from ResourceLifecycle) Transition(to ResourceLifecycle) error {
	if !from.CanTransition(to) {
		return resourceEdge(from, to)
	}
	return nil
}
func (from TaskState) Transition(to TaskState) error {
	if !from.CanTransition(to) {
		return taskEdge(from, to)
	}
	return nil
}
func (from AttemptState) Transition(to AttemptState) error {
	if !from.CanTransition(to) {
		return attemptEdge(from, to)
	}
	return nil
}
func (from LeaseState) Transition(to LeaseState) error {
	if !from.CanTransition(to) {
		return leaseEdge(from, to)
	}
	return nil
}
