//go:build linux

package syncer

import "golang.org/x/sys/unix"

func atomicExchange(source, destination string) (bool, error) {
	err := unix.Renameat2(unix.AT_FDCWD, source, unix.AT_FDCWD, destination, unix.RENAME_EXCHANGE)
	if err == nil {
		return true, nil
	}
	if err == unix.ENOSYS || err == unix.EINVAL || err == unix.ENOTSUP || err == unix.EXDEV {
		return false, nil
	}
	return false, err
}
