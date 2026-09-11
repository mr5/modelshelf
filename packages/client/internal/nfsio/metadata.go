// Package nfsio contains bounded compatibility handling for read-only NFS metadata.
package nfsio

import (
	"errors"
	"os"
	"runtime"
	"syscall"
)

// Metadata retries Linux EREMOTEIO once, on the same source. Linux 6.19+
// GET_DIR_DELEGATION against older Ganesha can fail the first GETATTR with
// OP_ILLEGAL; the next request has no delegation flag and succeeds.
// https://github.com/nfs-ganesha/nfs-ganesha/issues/1385
// Do not retry writes, timeouts, permission errors, or missing files here.
func Metadata[T any](read func() (T, error)) (T, error) {
	value, err := read()
	// EREMOTEIO is 121 on Linux; it is not defined on Darwin.
	if runtime.GOOS == "linux" && errors.Is(err, syscall.Errno(121)) {
		return read()
	}
	return value, err
}
func Stat(path string) (os.FileInfo, error) {
	return Metadata(func() (os.FileInfo, error) { return os.Stat(path) })
}
func Lstat(path string) (os.FileInfo, error) {
	return Metadata(func() (os.FileInfo, error) { return os.Lstat(path) })
}
func Open(path string) (*os.File, error) {
	return Metadata(func() (*os.File, error) { return os.Open(path) })
}
