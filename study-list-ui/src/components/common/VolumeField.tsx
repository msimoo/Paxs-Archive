import { useEffect, useRef } from 'react';
import { cn } from '@/lib/utils';

/**
 * A slow, low-contrast wireframe "volume" behind the header.
 *
 * Three deliberate constraints:
 *  1. **Lazy.** three.js is 600 KB and the study list must paint fast, so the
 *     library is fetched only after the page is idle — never as a bundle import.
 *  2. **Optional.** If WebGL is missing, the script fails, or the user asked for
 *     reduced motion, this renders nothing and the header still looks finished.
 *     A decorative element must never be able to break a clinical list.
 *  3. **Cheap.** One icosahedron, one point cloud, capped DPR, paused when the
 *     tab is hidden, and every GPU resource disposed on unmount.
 *
 * It reads `--paxs-accent` at draw time, so a theme flip re-colours it without
 * a re-mount.
 */

type ThreeModule = {
  Scene: new () => any;
  PerspectiveCamera: new (fov: number, aspect: number, near: number, far: number) => any;
  WebGLRenderer: new (options: { alpha: boolean; antialias: boolean }) => any;
  IcosahedronGeometry: new (radius: number, detail: number) => any;
  BufferGeometry: new () => any;
  BufferAttribute: new (array: Float32Array, itemSize: number) => any;
  PointsMaterial: new (options: Record<string, unknown>) => any;
  MeshBasicMaterial: new (options: Record<string, unknown>) => any;
  Mesh: new (geometry: any, material: any) => any;
  Points: new (geometry: any, material: any) => any;
  Color: new (value: string) => any;
};

let threePromise: Promise<ThreeModule | null> | null = null;

function loadThree(): Promise<ThreeModule | null> {
  if (threePromise) return threePromise;
  const pending = new Promise<ThreeModule | null>((resolve) => {
    const existing = (window as unknown as { THREE?: ThreeModule }).THREE;
    if (existing) {
      resolve(existing);
      return;
    }
    const script = document.createElement('script');
    script.src = '/viewer/three.min.js';
    script.async = true;
    script.onload = () => resolve((window as unknown as { THREE?: ThreeModule }).THREE ?? null);
    script.onerror = () => resolve(null);
    document.head.appendChild(script);
  });
  threePromise = pending.catch(() => null);
  return threePromise;
}

function accentColor(): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue('--paxs-accent').trim();
  return value || '#22d3ee';
}

export function VolumeField({ className }: { className?: string }) {
  const hostRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    // jsdom and locked-down browsers have no WebGL: skip before touching three.
    if (typeof window === 'undefined' || !('WebGLRenderingContext' in window)) return;

    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false;
    let disposed = false;
    let frame = 0;
    let cleanup: (() => void) | null = null;

    const start = (THREE: ThreeModule) => {
      if (disposed) return;
      // WebGL can be absent (headless, blocked GPU, ancient driver). This is
      // decoration: bail silently rather than raise into a clinical list.
      try {

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
      camera.position.set(0, 0, 6.4);

      const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
      renderer.setClearColor(0x000000, 0);
      host.appendChild(renderer.domElement);
      renderer.domElement.style.cssText = 'width:100%;height:100%;display:block;';

      const ink = new THREE.Color(accentColor());

      // Shell: a wireframe icosahedron reads as a scanned volume, not a logo.
      const shellGeometry = new THREE.IcosahedronGeometry(1.85, 1);
      const shellMaterial = new THREE.MeshBasicMaterial({
        color: ink,
        wireframe: true,
        transparent: true,
        opacity: 0.16,
      });
      const shell = new THREE.Mesh(shellGeometry, shellMaterial);
      scene.add(shell);

      // Core: sparse points inside the shell, so the form has depth.
      const count = 520;
      const positions = new Float32Array(count * 3);
      for (let i = 0; i < count; i += 1) {
        const radius = 1.1 + Math.random() * 0.62;
        const theta = Math.random() * Math.PI * 2;
        const phi = Math.acos(2 * Math.random() - 1);
        positions[i * 3] = radius * Math.sin(phi) * Math.cos(theta);
        positions[i * 3 + 1] = radius * Math.sin(phi) * Math.sin(theta);
        positions[i * 3 + 2] = radius * Math.cos(phi);
      }
      const cloudGeometry = new THREE.BufferGeometry();
      cloudGeometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      const cloudMaterial = new THREE.PointsMaterial({
        color: ink,
        size: 0.035,
        transparent: true,
        opacity: 0.55,
        depthWrite: false,
      });
      const cloud = new THREE.Points(cloudGeometry, cloudMaterial);
      scene.add(cloud);

      const resize = () => {
        const { clientWidth, clientHeight } = host;
        if (!clientWidth || !clientHeight) return;
        renderer.setSize(clientWidth, clientHeight, false);
        camera.aspect = clientWidth / clientHeight;
        camera.updateProjectionMatrix();
      };
      resize();

      const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
      observer?.observe(host);

      const draw = () => {
        renderer.render(scene, camera);
      };

      const tick = () => {
        if (disposed) return;
        shell.rotation.y += 0.0016;
        shell.rotation.x = Math.sin(Date.now() * 0.00012) * 0.12;
        cloud.rotation.y -= 0.0009;
        draw();
        frame = requestAnimationFrame(tick);
      };

      if (reduced) draw();
      else frame = requestAnimationFrame(tick);

      const onVisibility = () => {
        if (document.hidden) {
          cancelAnimationFrame(frame);
        } else if (!reduced && !disposed) {
          frame = requestAnimationFrame(tick);
        }
      };
      document.addEventListener('visibilitychange', onVisibility);

      cleanup = () => {
        cancelAnimationFrame(frame);
        document.removeEventListener('visibilitychange', onVisibility);
        observer?.disconnect();
        shellGeometry.dispose();
        shellMaterial.dispose();
        cloudGeometry.dispose();
        cloudMaterial.dispose();
        renderer.dispose();
        renderer.domElement.remove();
      };
      } catch {
        // No usable WebGL context — leave the header as it was.
        host.replaceChildren();
        cleanup = null;
      }
    };

    // Wait for idle so the first paint and the first API call win the CPU.
    const idle = (window as unknown as { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number })
      .requestIdleCallback;
    const handle = idle
      ? idle(() => void loadThree().then((three) => three && start(three)), { timeout: 1800 })
      : window.setTimeout(() => void loadThree().then((three) => three && start(three)), 900);

    return () => {
      disposed = true;
      (window as unknown as { cancelIdleCallback?: (h: number) => void }).cancelIdleCallback?.(handle as number);
      window.clearTimeout(handle as number);
      cleanup?.();
      host.replaceChildren();
    };
  }, []);

  return (
    <div
      ref={hostRef}
      aria-hidden="true"
      className={cn('pointer-events-none select-none', className)}
    />
  );
}
