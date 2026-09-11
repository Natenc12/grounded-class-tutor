// Friendly names for the API's request and success shapes. Every one is an alias into
// `schema.gen.ts`, which is generated from the API's own published schema, so each is
// field-for-field the pydantic model it is named after (src/gct/api/routers/*.py) - there is no
// hand-written copy here to drift. The error side is NOT here: see `envelope.ts`.
import type { components, paths } from './schema.gen';

type Schemas = components['schemas'];

// `POST /classes`
export type NewClass = Schemas['NewClass'];
export type ClassCreated = Schemas['ClassCreated'];

// `POST /files` (a multipart form; its field names are this type's keys) and `GET /files/{file_id}`
export type UploadForm = Schemas['Body_upload_file_files_post'];
export type UploadAccepted = Schemas['UploadAccepted'];
export type FileStatusResponse = Schemas['FileStatusResponse'];

// `POST /ask`
export type AskRequest = Schemas['AskRequest'];
export type AskResponse = Schemas['AskResponse'];
export type CitationOut = Schemas['CitationOut'];
export type CoverageOut = Schemas['CoverageOut'];
export type IntegrityOut = Schemas['IntegrityOut'];
/** The four grounding states a 200 can carry. ERROR is never one of them: it is an envelope. */
export type GroundingState = AskResponse['state'];

/** A path the published schema declares. */
export type ApiPath = keyof paths;
