package scheduler

import (
	"context"
	"testing"
	"time"
)

// accountTaskGap 的回归：跨任务批次处理同一账号要保底间隔，同批次与不同账号不等待。
// 现场是 9/21 点签到与旅行同槽并行，排第一的账号被两串请求同秒打中 → 余额查询 429。

// TestAccountGateSpacesDifferentKinds 不同批次之间等待 accountTaskGap。
func TestAccountGateSpacesDifferentKinds(t *testing.T) {
	old := accountTaskGap
	accountTaskGap = 40 * time.Millisecond
	t.Cleanup(func() { accountTaskGap = old })

	s := New(Config{})
	ctx := context.Background()
	if !s.accountGate(ctx, "u1", taskCheckin) {
		t.Fatal("首批应直接放行")
	}
	start := time.Now()
	if !s.accountGate(ctx, "u1", taskTravel) {
		t.Fatal("ctx 未取消，应返回 true")
	}
	if d := time.Since(start); d < 30*time.Millisecond {
		t.Errorf("跨批次间隔 %v，want >= ~40ms", d)
	}
}

// TestAccountGateSameKindNoWait 同一批次内不再叠加间隔（各自已有 accountGap 控节奏）。
func TestAccountGateSameKindNoWait(t *testing.T) {
	old := accountTaskGap
	accountTaskGap = time.Hour
	t.Cleanup(func() { accountTaskGap = old })

	s := New(Config{})
	ctx := context.Background()
	if !s.accountGate(ctx, "u1", taskCheckin) {
		t.Fatal("首批应直接放行")
	}
	start := time.Now()
	if !s.accountGate(ctx, "u1", taskCheckin) {
		t.Fatal("ctx 未取消，应返回 true")
	}
	if d := time.Since(start); d > 100*time.Millisecond {
		t.Errorf("同批次不应等待，实际 %v", d)
	}
}

// TestAccountGateDifferentAccounts 不同账号互不影响（批次级并行仍在）。
func TestAccountGateDifferentAccounts(t *testing.T) {
	old := accountTaskGap
	accountTaskGap = time.Hour
	t.Cleanup(func() { accountTaskGap = old })

	s := New(Config{})
	ctx := context.Background()
	start := time.Now()
	if !s.accountGate(ctx, "u1", taskCheckin) {
		t.Fatal("u1 首批应直接放行")
	}
	if !s.accountGate(ctx, "u2", taskTravel) {
		t.Fatal("u2 不应为 u1 的预约买单")
	}
	if d := time.Since(start); d > 100*time.Millisecond {
		t.Errorf("不同账号不应互相等待，实际 %v", d)
	}
}

// TestAccountGateCancels 等待期间取消立即返回 false，不把停机拖成 sleep。
func TestAccountGateCancels(t *testing.T) {
	old := accountTaskGap
	accountTaskGap = time.Hour
	t.Cleanup(func() { accountTaskGap = old })

	s := New(Config{})
	if !s.accountGate(context.Background(), "u1", taskCheckin) {
		t.Fatal("首批应直接放行")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	start := time.Now()
	if s.accountGate(ctx, "u1", taskTravel) {
		t.Fatal("取消后应返回 false")
	}
	if d := time.Since(start); d > 500*time.Millisecond {
		t.Errorf("取消应迅速返回，实际 %v", d)
	}
}
