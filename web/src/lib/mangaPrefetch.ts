/** Bound speculative acquisition to three neighbors; retain loaded pages separately. */
export function mangaNear(index: number, focus: number, direction: 1 | -1): boolean {
  const distance = (index - focus) * direction;
  return distance >= -1 && distance <= 2;
}
