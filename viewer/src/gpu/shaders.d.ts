// A .wgsl import is the shader's source, minified by the build.
declare module '*.wgsl' {
    const source: string;
    export default source;
}
