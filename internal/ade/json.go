package ade

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

type Object = map[string]any

func ReadJSON(path string) (Object, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var value Object
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return nil, fmt.Errorf("invalid JSON file %s: %w", path, err)
	}
	if value == nil {
		return nil, fmt.Errorf("invalid JSON file %s: JSON value must be an object", path)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, fmt.Errorf("invalid JSON file %s: trailing JSON content", path)
	}
	return value, nil
}

func DecodeJSON(data []byte) (Object, error) {
	var value Object
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if value == nil {
		return nil, errors.New("JSON value must be an object")
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, errors.New("trailing JSON content")
	}
	return value, nil
}

func MarshalPretty(value any) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

func WriteJSON(path string, value any, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		return err
	}
	data, err := MarshalPretty(value)
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".ade-json-")
	if err != nil {
		return err
	}
	temporary := file.Name()
	defer os.Remove(temporary)
	if _, err := file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err := file.Chmod(mode); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}

func Lock(path string, shared bool, fn func() error) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		return err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	defer file.Close()
	mode := syscall.LOCK_EX
	if shared {
		mode = syscall.LOCK_SH
	}
	if err := syscall.Flock(int(file.Fd()), mode); err != nil {
		return err
	}
	defer syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
	return fn()
}

func CanonicalJSON(value any, ascii, spaced bool) ([]byte, error) {
	var buffer bytes.Buffer
	if err := writeCanonical(&buffer, reflect.ValueOf(value), ascii, spaced); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

func writeCanonical(out *bytes.Buffer, value reflect.Value, ascii, spaced bool) error {
	if !value.IsValid() {
		out.WriteString("null")
		return nil
	}
	for value.Kind() == reflect.Interface || value.Kind() == reflect.Pointer {
		if value.IsNil() {
			out.WriteString("null")
			return nil
		}
		value = value.Elem()
	}
	if value.Type() == reflect.TypeOf(json.Number("")) {
		out.WriteString(value.Interface().(json.Number).String())
		return nil
	}
	separator := ","
	colon := ":"
	if spaced {
		separator, colon = ", ", ": "
	}
	switch value.Kind() {
	case reflect.String:
		encoded, err := jsonString(value.String(), ascii)
		if err != nil {
			return err
		}
		out.WriteString(encoded)
	case reflect.Bool:
		out.WriteString(strconv.FormatBool(value.Bool()))
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		out.WriteString(strconv.FormatInt(value.Int(), 10))
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		out.WriteString(strconv.FormatUint(value.Uint(), 10))
	case reflect.Float32, reflect.Float64:
		out.WriteString(strconv.FormatFloat(value.Float(), 'g', -1, value.Type().Bits()))
	case reflect.Slice, reflect.Array:
		if value.Kind() == reflect.Slice && value.IsNil() {
			out.WriteString("null")
			return nil
		}
		out.WriteByte('[')
		for i := 0; i < value.Len(); i++ {
			if i > 0 {
				out.WriteString(separator)
			}
			if err := writeCanonical(out, value.Index(i), ascii, spaced); err != nil {
				return err
			}
		}
		out.WriteByte(']')
	case reflect.Map:
		if value.IsNil() {
			out.WriteString("null")
			return nil
		}
		if value.Type().Key().Kind() != reflect.String {
			return errors.New("JSON object keys must be strings")
		}
		keys := value.MapKeys()
		sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
		out.WriteByte('{')
		for i, key := range keys {
			if i > 0 {
				out.WriteString(separator)
			}
			encoded, err := jsonString(key.String(), ascii)
			if err != nil {
				return err
			}
			out.WriteString(encoded)
			out.WriteString(colon)
			if err := writeCanonical(out, value.MapIndex(key), ascii, spaced); err != nil {
				return err
			}
		}
		out.WriteByte('}')
	default:
		if value.CanInterface() {
			data, err := json.Marshal(value.Interface())
			if err != nil {
				return err
			}
			var decoded any
			decoder := json.NewDecoder(bytes.NewReader(data))
			decoder.UseNumber()
			if err := decoder.Decode(&decoded); err != nil && err != io.EOF {
				return err
			}
			if reflect.TypeOf(decoded) == value.Type() {
				return errors.New("unsupported JSON value")
			}
			return writeCanonical(out, reflect.ValueOf(decoded), ascii, spaced)
		}
		return fmt.Errorf("unsupported JSON value %s", value.Kind())
	}
	return nil
}

func jsonString(value string, ascii bool) (string, error) {
	var encoded bytes.Buffer
	encoder := json.NewEncoder(&encoded)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return "", err
	}
	result := strings.TrimSuffix(encoded.String(), "\n")
	if !ascii {
		return result, nil
	}
	var out strings.Builder
	for _, char := range result {
		if char <= 0x7f {
			out.WriteRune(char)
		} else if char <= 0xffff {
			fmt.Fprintf(&out, "\\u%04x", char)
		} else {
			r := char - 0x10000
			fmt.Fprintf(&out, "\\u%04x\\u%04x", 0xd800+(r>>10), 0xdc00+(r&0x3ff))
		}
	}
	return out.String(), nil
}

func Digest(value any) string {
	data, _ := CanonicalJSON(value, true, true)
	hash := sha256.Sum256(data)
	return fmt.Sprintf("%x", hash)
}

func Require(condition bool, message string) error {
	if !condition {
		return fmt.Errorf("%s", message)
	}
	return nil
}

func randomID() string {
	var data [16]byte
	if _, err := rand.Read(data[:]); err != nil {
		panic(err)
	}
	return fmt.Sprintf("%x", data[:])
}

func randomUUID() string {
	var data [16]byte
	if _, err := rand.Read(data[:]); err != nil {
		panic(err)
	}
	data[6] = (data[6] & 0x0f) | 0x40
	data[8] = (data[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		data[0:4], data[4:6], data[6:8], data[8:10], data[10:16])
}
