// Command zeonica-runner executes a Neura-generated (and argument-patched)
// kernel program on the Zeonica simulator and dumps the resulting memory
// image and return value as JSON.
//
// It is kernel-agnostic: inputs come from the memory layout JSON written by
// tools/align.py, so no per-kernel tile placement is hard-coded here.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/sarchlab/akita/v4/sim"
	"github.com/sarchlab/zeonica/core"
	"github.com/sarchlab/zeonica/runtimecfg"
)

type memoryLayout struct {
	Kernel        string  `json:"kernel"`
	MemoryWords   int     `json:"memory_words"`
	ReturnsValue  bool    `json:"returns_value"`
	InitialMemory []int64 `json:"initial_memory"`
}

type result struct {
	Simulator   string  `json:"simulator"`
	Kernel      string  `json:"kernel"`
	Return      int32   `json:"return"`
	Memory      []int32 `json:"memory"`
	Cycles      uint64  `json:"cycles"`
	WallSeconds float64 `json:"wall_seconds"`
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "zeonica-runner:", err)
		os.Exit(2)
	}
}

func be32(v uint32) []byte {
	return []byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)}
}

func main() {
	programPath := flag.String("program", "", "argument-patched Neura instruction YAML")
	layoutPath := flag.String("memory", "", "memory layout JSON from tools/align.py")
	archPath := flag.String("arch", "", "Zeonica arch spec YAML")
	outPath := flag.String("out", "", "output result JSON")
	quiet := flag.Bool("quiet", true, "suppress Zeonica's per-word preload chatter on stdout")
	flag.Parse()
	if *programPath == "" || *layoutPath == "" || *archPath == "" || *outPath == "" {
		flag.Usage()
		os.Exit(2)
	}

	raw, err := os.ReadFile(*layoutPath)
	must(err)
	var layout memoryLayout
	must(json.Unmarshal(raw, &layout))

	rt, err := runtimecfg.LoadRuntime(*archPath, layout.Kernel)
	must(err)
	// Keep Zeonica's JSON trace next to the result instead of creating
	// <kernel>.json.log in the working directory; drop it when tracing is off.
	keepTrace := rt.Config.LoggingEnabled && rt.Config.EnableTrace
	rt.Config.LogPath = *outPath + ".trace.log"
	logFile, err := rt.InitTraceLogger(core.LevelTrace)
	must(err)

	program := core.LoadProgramFileFromYAML(*programPath)
	if len(program) == 0 {
		must(fmt.Errorf("no PE programs found in %s", *programPath))
	}

	width, height := rt.Config.Columns, rt.Config.Rows
	mapped := 0
	for x := 0; x < width; x++ {
		for y := 0; y < height; y++ {
			if prog, ok := program[fmt.Sprintf("(%d,%d)", x, y)]; ok {
				rt.Driver.MapProgram(prog, [2]int{x, y})
				mapped++
			}
		}
	}
	if mapped != len(program) {
		must(fmt.Errorf("program uses PEs outside the %dx%d array (%d of %d mapped)",
			width, height, mapped, len(program)))
	}

	// Preload the shared memory image (word addressed, big-endian bytes).
	restore := silenceStdout(*quiet)
	for addr, v := range layout.InitialMemory {
		rt.Driver.PreloadSharedMemory(0, 0, be32(uint32(int32(v))), uint32(addr))
	}
	restore()

	for x := 0; x < width; x++ {
		for y := 0; y < height; y++ {
			tile := rt.Device.GetTile(x, y)
			rt.Engine.Schedule(sim.MakeTickEvent(tile.GetTickingComponent(), 0))
		}
	}

	start := time.Now()
	rt.Driver.Run()
	wall := time.Since(start).Seconds()

	res := result{
		Simulator:   "zeonica",
		Kernel:      layout.Kernel,
		Memory:      make([]int32, layout.MemoryWords),
		Cycles:      uint64(float64(rt.Engine.CurrentTime())*1e9 + 0.5),
		WallSeconds: wall,
	}
	for addr := 0; addr < layout.MemoryWords; addr++ {
		res.Memory[addr] = int32(rt.Driver.ReadSharedMemory(0, 0, uint32(addr)))
	}
	// The return value register is shared between all cores.
	res.Return = int32(rt.Device.GetTile(0, 0).GetRetVal())

	must(runtimecfg.CloseTraceLog(logFile))
	if !keepTrace {
		_ = os.Remove(rt.Config.LogPath)
	}

	out, err := json.Marshal(res)
	must(err)
	must(os.WriteFile(*outPath, out, 0o644))
	fmt.Printf("zeonica: kernel=%s cycles=%d wall=%.2fs\n", layout.Kernel, res.Cycles, wall)
}

// silenceStdout temporarily redirects os.Stdout to /dev/null.
func silenceStdout(enable bool) func() {
	if !enable {
		return func() {}
	}
	saved := os.Stdout
	devnull, err := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	if err != nil {
		return func() {}
	}
	os.Stdout = devnull
	return func() {
		os.Stdout = saved
		_ = devnull.Close()
	}
}
