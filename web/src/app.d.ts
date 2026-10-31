// See https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare global {
	namespace App {
		// interface Error {}
		interface Locals {
			/** set by hooks.server.ts when a broker agent key opened the page gate (read-only) */
			machine?: import('#lib/server/machine.ts').MachineIdentity;
		}
		// interface PageData {}
		interface PageState { kuraLightbox?: boolean; lb?: number; lbp?: number; }
		// interface Platform {}
	}
}

export {};
