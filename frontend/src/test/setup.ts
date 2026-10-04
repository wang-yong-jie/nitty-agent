import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

afterEach(cleanup);
Object.defineProperty(window, 'matchMedia', { writable: true, value: vi.fn().mockImplementation(query => ({
  matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
  addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
})) });
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };

// jsdom 不计算主题变量对应的行高；给自动伸缩文本框提供有限的测量基值。
const measurementStyle = document.createElement('style');
measurementStyle.textContent = 'textarea { box-sizing: border-box; font-size: 14px; line-height: 20px; padding: 0px; border-width: 0px; }';
document.head.appendChild(measurementStyle);
