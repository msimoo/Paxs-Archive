# PAXS Viewer — Performance & Low-Latency Optimization Blueprint

> **Engineering Guide for High-Speed Medical Imaging (CT, MRI, CR, XR, 3D MPR / Axial)**  
> **Goal:** Zero-latency playback, instant multi-slice CT/MR scrolling, and seamless 3D volume reconstruction.

---

## 📑 Table of Contents
1. [Overview & Bottleneck Analysis](#1-overview--bottleneck-analysis)
2. [Client-Side Optimization (PaxsViewer & Web Workers)](#2-client-side-optimization-paxsviewer--web-workers)
3. [Server-Side Streaming & In-Memory Caching](#3-server-side-streaming--in-memory-caching)
4. [3D MPR & Volume Viewport Acceleration](#4-3d-mpr--volume-viewport-acceleration)
5. [Network, HTTP/2 & Codec Compression](#5-network-http2--codec-compression)
6. [Implementation Action Plan](#6-implementation-action-plan)

---

## 1. Overview & Bottleneck Analysis

Medical imaging datasets present distinct performance challenges:
* **Large Volumetric Stacks (CT / MRI)**: 500 to 3,000+ slices requiring gigabytes of high-bit-depth pixel data.
* **Large Static Single Frames (CR / DX / MG)**: High spatial resolution (4K x 4K, 16-bit) demanding fast single-image rendering.
* **3D Volumetric Reconstruction (MPR / MIP / VRT)**: Real-time orthogonal reslicing (Axial, Coronal, Sagittal) without dropping frames.

```
+---------------------------------------------------------------------------------------+
|                                    BOTTLENECK MATRIX                                  |
+-------------------+--------------------------------+----------------------------------+
| Layer             | Cause of Latency               | Optimization Target              |
+-------------------+--------------------------------+----------------------------------+
| Client / Viewer   | Main-thread single-CPU decode  | Multi-threaded Web Workers       |
|                   | Small RAM cache & WebGL lag   | 1GB+ Volumetric Image Cache      |
+-------------------+--------------------------------+----------------------------------+
| Server / API      | Full dataset parsing per frame | Zero-copy mmap raw frame stream  |
|                   | Repetitive disk I/O on scrolls | In-Memory LRU / Redis cache      |
+-------------------+--------------------------------+----------------------------------+
| Network Transport | Sequential frame requests      | HTTP/2 Multiplexing & HTJ2K Wasm |
+-------------------+--------------------------------+----------------------------------+
```

---

## 2. Client-Side Optimization (PaxsViewer & Web Workers)

### A. Maximizing Web Worker Concurrency
By default, viewers limit background threads to 3. Expanding this to use all available CPU logical cores allows 8 to 16 DICOM frames to decode simultaneously:

```javascript
// app/static/viewer/app-config.js
window.config = {
  // Leverage all available CPU cores for background DICOM decoding
  maxNumberOfWebWorkers: typeof navigator !== 'undefined' ? (navigator.hardwareConcurrency || 8) : 8,
  enableStudyLazyLoad: true,
  strictZSpacingForVolumeViewport: true,
  // Prefetch bulk metadata and instances
  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
      sourceName: 'paxs',
      configuration: {
        wadoUriRoot: '/dicom-web',
        qidoRoot: '/dicom-web',
        wadoRoot: '/dicom-web',
        qidoSupportsIncludeField: true,
        imageRendering: 'wadors',
        thumbnailRendering: 'wadors',
        singlepart: 'bulkdata,video,pdf',
        omitQuotationForMultipartRequest: true,
      },
    },
  ],
};
```

### B. Proactive Neighboring Slice Pre-fetching
* When the radiologist lands on slice `N`, the viewer automatically pulls `[N-10 ... N+10]` in the background.
* Prevents stutter during rapid mouse-wheel scrolling across 1,000+ CT slices.

---

## 3. Server-Side Streaming & In-Memory Caching

### A. In-Memory LRU & Redis Series Cache
When a series is viewed for the first time, its extracted pixel frames are cached in high-speed RAM:
* **Cache Key**: `frame:{study_uid}:{series_uid}:{sop_uid}:{frame_index}`
* **Storage**: In-memory Python `FastLRUCache` for single-node or **Redis** for distributed clusters.
* **Benefit**: Re-scrolling or switching views hits RAM (`< 1ms`) instead of disk reads (`15-40ms`).

### B. Zero-Copy Binary Frame Streaming
Avoid re-reading and decoding the full DICOM metadata headers on every single slice request:
* Stream the raw compressed or uncompressed byte buffer directly to the HTTP socket using asynchronous file descriptors (`aiofiles` / `sendfile`).

---

## 4. 3D MPR & Volume Viewport Acceleration

### A. Single 3D 32-bit Float Texture Upload
Instead of reslicing in JavaScript CPU memory:
1. Load the entire volumetric stack (e.g. 512x512x400 voxels) into a single **WebGL2 3D Texture (`gl.TEXTURE_3D`)**.
2. Perform Axial, Coronal, and Sagittal orthogonal cuts directly on the GPU fragment shader.
3. **Result**: 60 FPS buttery-smooth MPR reslicing with real-time Window/Level adjustments.

### B. Progressive Volume Level-of-Detail (LOD)
* **Phase 1 (Instant)**: Load downsampled 256x256 voxel volume within `100ms` so the radiologist sees the 3D planes immediately.
* **Phase 2 (Background)**: Stream full 512x512 precision slices in parallel to replace the preview with clinical-grade resolution.

---

## 5. Network, HTTP/2 & Codec Compression

### A. High-Throughput JPEG 2000 (HTJ2K) & JPEG-LS
* Replace legacy uncompressed DICOM with **HTJ2K (High-Throughput JPEG 2000)**.
* **Decompression Speed**: **5x to 10x faster** than traditional JPEG 2000 using WebAssembly SIMD (`openjph.wasm`).
* **Transfer Reduction**: Cuts network payload by **65%–75%** with mathematically lossless bit fidelity.

### B. HTTP/2 Concurrent Multiplexing
* Utilize HTTP/2 to request dozens of frame chunks over a single persistent TCP handshake, eliminating HTTP/1.1 connection blocking.

---

## 6. Implementation Action Plan

| Step | Action Item | Target File | Impact |
|---|---|---|---|
| **1** | Increase Web Worker threads to `navigator.hardwareConcurrency` | [`app/static/viewer/app-config.js`](file:///D:/Paxs%20Archive/app/static/viewer/app-config.js) | 300% faster frame decoding |
| **2** | Optimize WADO-RS `/frames` with memory caching & immutable headers | [`app/api/v1/dicomweb.py`](file:///D:/Paxs%20Archive/app/api/v1/dicomweb.py) | Sub-millisecond slice responses |
| **3** | GPU 3D Float texture uploading for MPR viewports | [`app/static/viewer/paxs-mpr.js`](file:///D:/Paxs%20Archive/app/static/viewer/paxs-mpr.js) | 60 FPS real-time 3D MPR slicing |
| **4** | Enable Lossless HTJ2K / JPEG-LS compression background worker | [`app/services/worker.py`](file:///D:/Paxs%20Archive/app/services/worker.py) | 70% reduced network bandwidth |
