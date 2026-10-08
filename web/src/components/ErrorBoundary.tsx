import { Component, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
  onReset?: () => void;
}

interface ErrorBoundaryState {
  failed: boolean;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch() {
    // The UI deliberately avoids rendering exception details or server data.
  }

  private reset = () => {
    this.props.onReset?.();
    this.setState({ failed: false });
  };

  render() {
    if (this.state.failed) {
      return (
        <main className="fatal-state" role="alert">
          <h1>This view could not be loaded</h1>
          <p>Reload the page to try again.</p>
          <button type="button" className="primary-button" onClick={this.reset}>Reload</button>
        </main>
      );
    }
    return this.props.children;
  }
}
