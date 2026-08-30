package mount

import "testing"

func TestValidatedNFSVersion(t *testing.T) {
	tests := []struct {
		input string
		want  string
		ok    bool
	}{
		{"", "4.1", true},
		{"4.1", "4.1", true},
		{"4.2", "4.2", true},
		{"4", "", false},
		{"3", "", false},
		{"4.2,soft", "", false},
	}
	for _, test := range tests {
		got, err := validatedNFSVersion(test.input)
		if test.ok && (err != nil || got != test.want) {
			t.Errorf("validatedNFSVersion(%q) = %q, %v", test.input, got, err)
		}
		if !test.ok && err == nil {
			t.Errorf("validatedNFSVersion(%q) unexpectedly returned %q", test.input, got)
		}
	}
}

func TestValidatedNFSSource(t *testing.T) {
	tests := []struct {
		host       string
		exportPath string
		want       string
		valid      bool
	}{
		{host: "modelshelf.internal", exportPath: "/modelshelf", want: "modelshelf.internal:/modelshelf", valid: true},
		{host: "fd00::10", exportPath: "/modelshelf", want: "[fd00::10]:/modelshelf", valid: true},
		{host: "bad\nWhere=/escape", exportPath: "/modelshelf", valid: false},
		{host: "modelshelf.internal", exportPath: "/modelshelf\nOptions=rw", valid: false},
		{host: "modelshelf.internal", exportPath: "relative", valid: false},
	}
	for _, test := range tests {
		got, err := validatedNFSSource(test.host, test.exportPath)
		if test.valid && (err != nil || got != test.want) {
			t.Errorf("validatedNFSSource(%q, %q) = %q, %v", test.host, test.exportPath, got, err)
		}
		if !test.valid && err == nil {
			t.Errorf("validatedNFSSource(%q, %q) unexpectedly accepted %q", test.host, test.exportPath, got)
		}
	}
}

func TestValidateMountTargetRejectsUnitFileInjection(t *testing.T) {
	if err := validateMountTarget("/mnt/modelshelf"); err != nil {
		t.Fatal(err)
	}
	if err := validateMountTarget("/mnt/modelshelf\nOptions=rw"); err == nil {
		t.Fatal("target with newline was accepted")
	}
}
