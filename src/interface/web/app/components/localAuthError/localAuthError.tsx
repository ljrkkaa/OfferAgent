export const LOCAL_AUTH_ERROR =
    "Authentication required. Configure KHOJ_API_KEY in the client or enable local anonymous mode.";

export default function LocalAuthError() {
    return (
        <p
            role="alert"
            className="m-4 rounded-md border border-destructive p-3 text-sm text-destructive"
        >
            {LOCAL_AUTH_ERROR}
        </p>
    );
}
