package nfsio

import (
	"errors"
	"os"
	"runtime"
	"syscall"
	"testing"
)

func TestBoundedMetadataRetry(t *testing.T) {
	for _, err := range []error{os.ErrNotExist, os.ErrPermission, syscall.Errno(121)} {
		calls := 0
		_, got := Metadata(func() (int, error) { calls++; return 0, err })
		want := 1
		if runtime.GOOS == "linux" && errors.Is(err, syscall.Errno(121)) {
			want = 2
		}
		if calls != want || !errors.Is(got, err) {
			t.Fatalf("calls=%d want=%d error=%v", calls, want, got)
		}
	}
}
