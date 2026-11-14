// Positive control for Apple's Metal API validation (MTL_DEBUG_LAYER=1).
//
// It binds a buffer at an offset past its end on a compute encoder. Only the API validation
// layer (its MTLDebug* encoder proxies) checks encoder arguments, so with the layer the call
// fails an assertion and aborts; without it the call is not checked and the probe exits 0.
// Checks the Metal framework always performs (such as rejecting a zero-sized texture
// descriptor) would not tell the two apart. Nothing is committed to the GPU.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <objc/runtime.h>
#include <cstdio>

int main() {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device) {
            std::fprintf(stderr, "No Metal device\n");
            return 3;
        }
        id<MTLCommandQueue> queue = [device newCommandQueue];
        id<MTLCommandBuffer> commandBuffer = [queue commandBuffer];
        id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
        std::printf("device: %s; encoder class: %s\n", device.name.UTF8String,
                    class_getName([(id)encoder class]));
        std::fflush(stdout);
        id<MTLBuffer> buffer = [device newBufferWithLength:16 options:MTLResourceStorageModeShared];
        [encoder setBuffer:buffer offset:4096 atIndex:0];  // out of range: only validation notices
        [encoder endEncoding];
        std::printf("out-of-range buffer offset accepted: Metal API validation inactive\n");
        return 0;
    }
}
