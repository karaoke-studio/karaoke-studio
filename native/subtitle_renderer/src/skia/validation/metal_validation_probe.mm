// Positive control for Apple's Metal API validation (MTL_DEBUG_LAYER=1).
//
// It deliberately requests a zero-sized texture, which the validation layer rejects with an
// assertion that aborts the process. Without the layer the call just returns nil and the probe
// exits 0 saying so. Tests run this in the same environment as the preview so that "no Metal
// validation errors" there means validation was really active, not merely requested.
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
        // With the layer active the device is wrapped by a debug proxy class.
        std::printf("device: %s (%s)\n", device.name.UTF8String, class_getName([device class]));
        std::fflush(stdout);
        MTLTextureDescriptor* descriptor =
            [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                               width:0
                                                              height:0
                                                           mipmapped:NO];
        id<MTLTexture> texture = [device newTextureWithDescriptor:descriptor];
        std::printf("invalid zero-sized texture request returned %s: Metal API validation inactive\n",
                    texture ? "an object" : "nil");
        return 0;
    }
}
