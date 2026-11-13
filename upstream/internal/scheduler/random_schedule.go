package scheduler

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"math/rand"
	"time"

	"workbuddy2api/internal/cst"
)

type scheduledAccountKey struct{}

// accountGap adds jitter to the original minimum pacing, only on scheduled
// traversals. Manual calls and a zero range keep their existing behavior.
func (s *Scheduler) accountGap(ctx context.Context, base time.Duration) time.Duration {
	if ctx.Value(scheduledAccountKey{}) != true || s.cfg.AccountDelayMaxSeconds <= 0 {
		return base
	}
	lo, hi := s.cfg.AccountDelayMinSeconds, s.cfg.AccountDelayMaxSeconds
	if lo < 0 || hi < lo || hi > 600 {
		return base
	}
	seconds := lo + rand.Intn(hi-lo+1)
	return base + time.Duration(seconds)*time.Second
}

func (s *Scheduler) accountScriptArgs(ctx context.Context, args []string) []string {
	if ctx.Value(scheduledAccountKey{}) != true || s.cfg.AccountDelayMaxSeconds <= 0 {
		return args
	}
	return append(args, "--account-delay-min", fmt.Sprint(s.cfg.AccountDelayMinSeconds),
		"--account-delay-max", fmt.Sprint(s.cfg.AccountDelayMaxSeconds))
}

// scheduledSlot derives a daily random offset from a stable private seed. The
// same slot keeps its time after a restart, while each date, hour, task kind and
// deployment gets its own draw. There is no mutable random state to lose.
//
// 钟点按 CST 解释（cst.At），不跟随 day.Location()：排程小时是上游自然日的小时，
// 宿主时区一换，同一份配置就落到不同时刻。
func (s *Scheduler) scheduledSlot(day time.Time, hour int, kind taskKind) time.Time {
	at := cst.At(day, hour, 0, 0)
	minutes := s.cfg.RandomDelayMinutes
	if minutes <= 0 {
		return at
	}
	// Keep every draw inside its original hour and natural day. At 60 minutes
	// the last possible offset is 59:59, avoiding overlap with the next slot.
	if minutes > 60 {
		minutes = 60
	}
	seconds := minutes * 60
	if seconds >= 3600 {
		seconds = 3599
	}
	identity := fmt.Sprintf("%s\x00%s\x00%d\x00%d", s.cfg.RandomDelaySeed,
		at.Format("2006-01-02"), hour, kind)
	digest := sha256.Sum256([]byte(identity))
	offset := binary.BigEndian.Uint64(digest[:8]) % uint64(seconds+1)
	return at.Add(time.Duration(offset) * time.Second)
}

func (s *Scheduler) nextScheduledFire(now time.Time, sch schedule) time.Time {
	if s.cfg.RandomDelayMinutes <= 0 {
		return nextFire(now, sch.hours)
	}
	var earliest time.Time
	// 日起点按 CST：否则「今天」的边界随宿主时区漂，跨日的两个槽位会被算到同一天。
	day := cst.At(now, 0, 0, 0)
	for i := 0; i < 2; i++ {
		for _, hour := range sch.hours {
			at := s.scheduledSlot(day.AddDate(0, 0, i), hour, sch.kind)
			if at.After(now) && (earliest.IsZero() || at.Before(earliest)) {
				earliest = at
			}
		}
	}
	return earliest
}
