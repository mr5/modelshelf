//go:build darwin

package syncer

import "golang.org/x/sys/unix"

func atomicExchange(source, destination string) (bool, error) {
	err := unix.RenamexNp(source, destination, unix.RENAME_SWAP)
	if err == nil {
		return true, nil
	}
	if err == unix.ENOSYS || err == unix.EINVAL || err == unix.ENOTSUP || err == unix.EXDEV {
		return false, nil
	}
	return false, err
}
