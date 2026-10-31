// See https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare global {
	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		interface PageState { kuraLightbox?: boolean; lb?: number; lbp?: number; }
		// interface Platform {}
	}
}

export {};
