//go:build !linux && !darwin

package syncer

func atomicExchange(_, _ string) (bool, error) {
	return false, nil
}
