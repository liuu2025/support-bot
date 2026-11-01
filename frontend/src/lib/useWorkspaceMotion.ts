import { useEffect, useRef } from "react";
import { attachSpotlight } from "./spotlight";

export function useWorkspaceMotion(enabled: boolean) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (enabled && root.current) return attachSpotlight(root.current);
  }, [enabled]);
  return root;
}
